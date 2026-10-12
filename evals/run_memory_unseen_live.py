"""Replay the frozen 24-case memory challenge through one real Kimi Agent.

Without --live this validates the frozen dataset and selection only. Live runs
use production chat persistence, automatic memory writes and hybrid retrieval;
the hidden Kimi key is never written to results. Each case has a fresh database.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.vector_memory import EmbeddingEndpoint, OpenAICompatibleEmbedder
from app.config import Settings
from app.team.models import MemberRole
from evals.run_memory_baseline import run_case, summarize
from evals.validate_memory_unseen import CASES_PATH, LOCK_PATH, validate_unseen

_REPO = Path(__file__).resolve().parents[1]
_RESULTS = _REPO / "evals" / "results" / "memory-unseen-live-v1"
SMOKE_CASE_IDS = (
    "dev_async_queue_distractor",
    "dev_consul_question_with_distractor",
    "personal_return_library_book",
    "personal_sibling_food",
)
_TRACKED = (
    "app/chat/memory.py",
    "app/chat/vector_memory.py",
    "app/chat/dispatch.py",
    "app/chat/store.py",
    "evals/run_memory_baseline.py",
    "evals/run_memory_unseen_live.py",
)


def select_cases(
    cases: list[dict],
    ids: list[str],
    smoke: bool,
    smoke_case_ids: tuple[str, ...] = SMOKE_CASE_IDS,
) -> list[dict]:
    if smoke and ids:
        raise ValueError("--smoke and --case cannot be combined")
    selected_ids = list(smoke_case_ids) if smoke else ids
    if not selected_ids:
        return cases
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("duplicate case ID")
    by_id = {case["id"]: case for case in cases}
    unknown = [case_id for case_id in selected_ids if case_id not in by_id]
    if unknown:
        raise ValueError(f"unknown case: {', '.join(unknown)}")
    return [by_id[case_id] for case_id in selected_ids]


def _fingerprint(extra_tracked: tuple[str, ...] = ()) -> dict:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "commit": commit.stdout.strip() or None,
        "dirty": bool(dirty.stdout),
        "sha256": {
            name: hashlib.sha256((_REPO / name).read_bytes()).hexdigest()
            for name in (*_TRACKED, *extra_tracked)
        },
    }


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _resume_prefix(run_dir: Path, manifest: dict, cases: list[dict]) -> list[dict]:
    previous = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    for field in (
        "dataset_sha256",
        "lock_sha256",
        "selected_case_ids",
        "agent",
        "variant",
        "embedding",
        "timeout_seconds",
    ):
        if previous.get(field) != manifest[field]:
            raise ValueError(f"incompatible resume run: {field}")
    if previous["code"]["sha256"] != manifest["code"]["sha256"]:
        raise ValueError("incompatible resume run: code changed")
    completed = []
    for case in cases:
        path = run_dir / f"{case['id']}.json"
        if not path.exists():
            break
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("case_id") != case["id"] or not result.get("complete"):
            break
        completed.append(result)
    return completed


async def run_live(
    cases: list[dict],
    *,
    output_root: Path = _RESULTS,
    resume_from: Path | None = None,
    timeout_seconds: int = 180,
    dataset_path: Path = CASES_PATH,
    lock_path: Path = LOCK_PATH,
    extra_tracked: tuple[str, ...] = (),
) -> Path:
    if timeout_seconds < 1:
        raise ValueError("timeout must be positive")
    settings = Settings()
    if settings.memory_embedding_mode != "on":
        raise ValueError("hybrid run requires CODECREW_MEMORY_EMBEDDING_MODE=on")
    endpoint = EmbeddingEndpoint(
        base_url=settings.memory_embedding_base_url,
        model=settings.memory_embedding_model,
        revision=settings.memory_embedding_revision,
        api_key=settings.memory_embedding_api_key.get_secret_value(),
        timeout_seconds=settings.memory_embedding_timeout_seconds,
    )
    if shutil.which(settings.kimi_cli_path) is None:
        raise RuntimeError(f"Kimi CLI unavailable: {settings.kimi_cli_path}")
    if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        raise RuntimeError("KIMI_MODEL_API_KEY is required; use --prompt-for-kimi-key")

    manifest = {
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "selected_case_ids": [case["id"] for case in cases],
        "agent": "kimi-code-cli/implementer",
        "variant": "hybrid",
        "prompt_note": "Production chat prompt plus single-Agent handoff_to=[] instruction",
        "code": _fingerprint(extra_tracked),
        "timeout_seconds": timeout_seconds,
        "embedding": {
            "model": endpoint.model,
            "revision": endpoint.revision,
            "fingerprint": endpoint.model_fingerprint,
            "min_similarity": settings.memory_embedding_min_similarity,
        },
    }
    if resume_from is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        run_dir = output_root.resolve() / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_json(
            run_dir / "manifest.json",
            {
                **manifest,
                "run_id": run_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        results: list[dict] = []
    else:
        run_dir = resume_from.resolve()
        results = _resume_prefix(run_dir, manifest, cases)

    embedder = OpenAICompatibleEmbedder(endpoint)
    for index, case in enumerate(cases[len(results) :], len(results) + 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        try:
            result = await run_case(
                case,
                None,
                variant="hybrid",
                agent_role=MemberRole.IMPLEMENTER,
                timeout_seconds=timeout_seconds,
                embedder=embedder,
                min_similarity=settings.memory_embedding_min_similarity,
            )
        except Exception as exc:  # noqa: BLE001 - preserve safe failure metadata for any CLI error
            # Infrastructure errors may contain request metadata; persist only the type.
            _write_json(
                run_dir / "infrastructure_failure.json",
                {
                    "case_id": case["id"],
                    "error_type": type(exc).__name__,
                },
            )
            _write_json(run_dir / "summary.json", summarize(results))
            raise RuntimeError(
                f"case {case['id']} failed ({type(exc).__name__}); results: {run_dir}"
            ) from None
        results.append(result)
        _write_json(run_dir / f"{case['id']}.json", result)
        _write_json(run_dir / "summary.json", summarize(results))
        if not result["complete"]:
            print(
                "Agent turn incomplete; stopping without scoring it as a memory miss.", flush=True
            )
            break
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call Kimi and cloud embedding")
    parser.add_argument("--smoke", action="store_true", help="run four fixed boundary cases")
    parser.add_argument("--case", action="append", default=[], help="select a case; repeatable")
    parser.add_argument(
        "--prompt-for-kimi-key",
        action="store_true",
        help="read Kimi Code key via hidden terminal input",
    )
    parser.add_argument("--resume-from", type=Path, help="continue a compatible, interrupted run")
    parser.add_argument("--output-root", type=Path, default=_RESULTS)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        cases = select_cases(validate_unseen(), args.case, args.smoke)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.live:
        print(
            f"Validated {len(cases)} frozen unseen cases; no model called. "
            "Use --live --prompt-for-kimi-key to run Kimi + embedding."
        )
        return
    if args.prompt_for_kimi_key:
        key = getpass.getpass("Kimi Code key: ").strip()
        if not key:
            parser.error("Kimi Code key cannot be empty")
        os.environ["KIMI_MODEL_API_KEY"] = key
    run_dir = asyncio.run(
        run_live(
            cases,
            output_root=args.output_root,
            resume_from=args.resume_from,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
