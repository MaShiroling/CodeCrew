"""Replay four attribution cases and save replies for semantic review.

Default mode validates only. --live uses isolated Kimi CLI and may consume quota.
No embedding API is used; answer correctness is not automatically scored.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.memory import AgentMemoryProposal
from app.config import Settings
from evals.run_memory_fidelity_live import run_live_case

_REPO = Path(__file__).resolve().parents[1]
CASES_PATH = _REPO / "evals" / "memory-answer-quality-v1.json"
ADJUDICATIONS_PATH = _REPO / "evals" / "memory-answer-quality-v2-adjudications.json"
_RESULTS = _REPO / "evals" / "results"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if set(fixture) != {"version", "cases"} or fixture["version"] != 1:
        raise ValueError("unsupported memory answer-quality fixture")
    cases = fixture["cases"]
    if not isinstance(cases, list) or len(cases) != 4:
        raise ValueError("memory answer-quality v1 requires four cases")
    ids = set()
    for case in cases:
        if set(case) != {
            "id", "domain", "source", "proposal", "live_expected_write",
            "expected_answer", "forbidden_inference",
        }:
            raise ValueError("invalid memory answer-quality case fields")
        if (not isinstance(case["id"], str) or case["id"] in ids
                or case["domain"] not in {"development", "personal"}
                or not case["id"].startswith(
                    "dev_" if case["domain"] == "development" else "personal_"
                )):
            raise ValueError("invalid or duplicate answer-quality case ID")
        ids.add(case["id"])
        if any(not isinstance(case[key], str) or not case[key].strip()
               for key in ("source", "expected_answer", "forbidden_inference")):
            raise ValueError("answer-quality source and labels must be nonempty")
        if not isinstance(case["live_expected_write"], bool):
            raise TypeError("answer-quality write expectation must be boolean")
        AgentMemoryProposal.model_validate(case["proposal"])
    if Counter(case["domain"] for case in cases) != {"development": 2, "personal": 2}:
        raise ValueError("answer-quality cases must balance development and personal")
    return cases


def load_cases_v2(
    base_path: Path = CASES_PATH, adjudications_path: Path = ADJUDICATIONS_PATH,
) -> list[dict]:
    """Apply the user's stricter answer policy without changing v1 run inputs."""
    cases = load_cases(base_path)
    labels = json.loads(adjudications_path.read_text(encoding="utf-8"))
    if (set(labels) != {"version", "base_fixture_sha256", "adjudications"}
            or labels["version"] != 2
            or labels["base_fixture_sha256"] != hashlib.sha256(base_path.read_bytes()).hexdigest()):
        raise ValueError("answer-quality v2 adjudications do not match v1 fixture")
    by_id = {case["id"]: dict(case) for case in cases}
    seen = set()
    for item in labels["adjudications"]:
        if set(item) != {"case_id", "forbidden_inference", "reason", "decided_at"}:
            raise ValueError("invalid answer-quality adjudication")
        case_id = item["case_id"]
        if case_id not in by_id or case_id in seen:
            raise ValueError("unknown or duplicate answer-quality adjudication")
        if any(not isinstance(item[key], str) or not item[key].strip()
               for key in ("forbidden_inference", "reason", "decided_at")):
            raise ValueError("answer-quality adjudication text must be nonempty")
        seen.add(case_id)
        by_id[case_id]["forbidden_inference"] = item["forbidden_inference"]
    return [by_id[case["id"]] for case in cases]


def summarize(results: list[dict]) -> dict:
    return {
        "case_count": len(results),
        "complete_count": sum(item["complete"] for item in results),
        "write_presence_matches": sum(item["write_presence_match"] is True for item in results),
        "answer_quality": "pending_semantic_review",
    }


async def run_live(
    cases: list[dict], output_root: Path, timeout_seconds: int, *, labels_version: int,
) -> Path:
    settings = Settings()
    if shutil.which(settings.kimi_cli_path) is None:
        raise RuntimeError(f"Kimi CLI unavailable: {settings.kimi_cli_path}")
    if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        raise RuntimeError("KIMI_MODEL_API_KEY is required for isolated Kimi evaluation")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    tracked = (
        "app/chat/dispatch.py", "app/chat/memory.py", "app/chat/store.py",
        "evals/run_memory_fidelity_live.py", "evals/run_memory_answer_quality.py",
    )
    manifest = {
        "run_id": run_id, "agent": "kimi-code-cli/implementer",
        "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "labels_version": labels_version,
        "adjudications_sha256": (
            hashlib.sha256(ADJUDICATIONS_PATH.read_bytes()).hexdigest()
            if labels_version == 2 else None
        ),
        "code_sha256": {
            name: hashlib.sha256((_REPO / name).read_bytes()).hexdigest()
            for name in tracked
        },
        "timeout_seconds": timeout_seconds,
        "answer_review": "pending_semantic_review",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = await run_live_case(case, timeout_seconds=timeout_seconds)
        result["expected_answer"] = case["expected_answer"]
        result["forbidden_inference"] = case["forbidden_inference"]
        result["answer_review"] = "pending_semantic_review"
        results.append(result)
        (run_dir / f"{case['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(
            json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if not result["complete"]:
            print("Agent turn incomplete; stopping without scoring it as an answer failure.",
                  flush=True)
            break
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--prompt-for-kimi-key", action="store_true")
    parser.add_argument("--case", help="run one case ID")
    parser.add_argument("--labels", choices=("v1", "v2"), default="v2")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    cases = load_cases_v2() if args.labels == "v2" else load_cases()
    if args.case is not None:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            parser.error("unknown answer-quality case")
    if not args.live:
        print(f"Validated {len(cases)} answer-quality cases ({args.labels} labels); "
              "no model called. "
              "Use --live to invoke Kimi.")
        return
    if args.timeout_seconds < 1:
        parser.error("timeout must be positive")
    if args.prompt_for_kimi_key:
        os.environ["KIMI_MODEL_API_KEY"] = getpass.getpass("Kimi Code key: ")
    output_root = args.output_root or _RESULTS / f"memory-answer-quality-{args.labels}"
    run_dir = asyncio.run(run_live(
        cases, output_root.resolve(), args.timeout_seconds,
        labels_version=int(args.labels[1:]),
    ))
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
