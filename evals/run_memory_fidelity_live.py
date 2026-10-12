"""Replay fidelity cases with one real chat Agent turn per case.

Without --live this only validates the fixture. Live mode uses the isolated Kimi
Code adapter and may consume model quota. It never calls the embedding service.
Results contain synthetic case text and are stored under the ignored evals/results.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents import AgentAdapter, KimiCodeAdapter
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.models import StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.config import Settings
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy
from evals.check_memory_fidelity import (
    ADJUDICATIONS_PATH,
    CASES_PATH,
    load_cases,
    load_live_cases_v2,
)
from evals.run_memory_baseline import _SingleAgentEvalDispatcher, _turn_result

_REPO = Path(__file__).resolve().parents[1]
_RESULTS = _REPO / "evals" / "results"


async def run_live_case(
    case: dict, adapter: AgentAdapter | None = None, *, timeout_seconds: int = 180,
) -> dict:
    """Use production dispatch and auto-write with a fresh SQLite room."""
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-fidelity-live-") as scratch:
        root = Path(scratch).resolve()
        store = StandaloneChatStore(SQLiteDatabase(root / "chat.sqlite3"))
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title=f"Fidelity eval {case['id']}", idempotency_key=uuid4())
        workspaces = StandaloneChatWorkspaceManager(root / "workspaces", root / "runtime")
        if adapter is None:
            settings = Settings()
            adapter = KimiCodeAdapter(
                worktree_root=workspaces.workspace_root,
                runtime_root=workspaces.runtime_root,
                policy=PermissionPolicy(allowed_paths=(".",)),
                executable=settings.kimi_cli_path,
                allow_standalone_chat=True,
            )
        runtime = StandaloneChatAgentRuntime(
            workspaces,
            {role: adapter for role in (
                MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER,
            )},
        )
        dispatcher = _SingleAgentEvalDispatcher(
            store, runtime, max_turns_per_thread=1, timeout_seconds=timeout_seconds,
        )
        await dispatcher.startup()
        try:
            human = next(member for member in room.members if member.role is MemberRole.HUMAN)
            agent = next(member for member in room.members
                         if member.role is MemberRole.IMPLEMENTER)
            source = store.append_message(StandaloneChatMessage(
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=human.member_id, recipient_ids=(agent.member_id,),
                content=case["source"].replace("@白金", "@月见"),
                idempotency_key=f"fidelity-eval:{case['id']}",
            ))
            dispatcher.enqueue(source)
            await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
            turn = _turn_result(store, source)
            memories = store.memory.list_active(room.room_id)
            all_turns = store.list_turns(room.room_id)
            complete = (turn["status"] == "succeeded" and len(all_turns) == 1
                        and all_turns[0].recipient_id == agent.member_id)
            rows = [{
                "content": memory.content, "kind": memory.kind,
                "origin": memory.origin,
                "source_message_id": str(memory.source_message_id),
                "source_quote": memory.content in source.message.content,
            } for memory in memories]
            expected = case["live_expected_write"]
            expected_kind = case.get("live_expected_kind", case["proposal"]["kind"])
            return {
                "case_id": case["id"], "domain": case["domain"],
                "complete": complete, "source": source.message.content,
                "expected_write": expected,
                "expected_kind": expected_kind if expected else None,
                "turn": turn, "memories": rows,
                "write_presence_match": bool(memories) == expected if complete else None,
                "kind_match": (all(memory.kind == expected_kind
                                   for memory in memories) if complete and expected else None),
                "content_review": "pending_human_review",
            }
        finally:
            await dispatcher.shutdown()


def summarize(results: list[dict]) -> dict:
    complete = [item for item in results if item["complete"]]
    return {
        "case_count": len(results), "complete_count": len(complete),
        "write_presence_matches": sum(item["write_presence_match"] for item in complete),
        "positive_cases_with_write": sum(
            item["expected_write"] and bool(item["memories"]) for item in complete
        ),
        "negative_cases_without_write": sum(
            not item["expected_write"] and not item["memories"] for item in complete
        ),
        "content_review": "pending_human_review",
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
        "app/chat/memory.py", "app/chat/dispatch.py", "app/chat/store.py",
        "evals/check_memory_fidelity.py", "evals/run_memory_fidelity_live.py",
    )
    manifest = {
        "run_id": run_id, "agent": "kimi-code-cli/implementer",
        "model_note": "Kimi Code adapter alias: kimi-for-coding",
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
        "prompt_note": "Production chat prompt plus single-Agent handoff_to=[] instruction",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = await run_live_case(case, timeout_seconds=timeout_seconds)
        results.append(result)
        (run_dir / f"{case['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(
            json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if not result["complete"]:
            print("Agent turn incomplete; stopping without counting it as a quality miss.",
                  flush=True)
            break
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--prompt-for-kimi-key", action="store_true")
    parser.add_argument("--case", help="run only one case ID")
    parser.add_argument("--labels", choices=("v1", "v2"), default="v2")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    cases = load_live_cases_v2() if args.labels == "v2" else load_cases()
    if args.case is not None:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            parser.error("unknown fidelity case")
    if not args.live:
        print(f"Validated {len(cases)} fidelity cases ({args.labels} labels); no model called. "
              "Use --live to invoke Kimi.")
        return
    if args.timeout_seconds < 1:
        parser.error("timeout must be positive")
    if args.prompt_for_kimi_key:
        os.environ["KIMI_MODEL_API_KEY"] = getpass.getpass("Kimi Code key: ")
    output_root = args.output_root or _RESULTS / f"memory-fidelity-{args.labels}"
    run_dir = asyncio.run(run_live(
        cases, output_root.resolve(), args.timeout_seconds,
        labels_version=int(args.labels[1:]),
    ))
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
