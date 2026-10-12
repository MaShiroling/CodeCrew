"""Two Kimi turns: auto-write an open_loop, close it, then verify after restart.

Default mode only validates the synthetic fixture. --live consumes Kimi quota,
but uses no embedding API. The key is read from a hidden terminal prompt or env.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents import AgentAdapter, KimiCodeAdapter
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.dispatch import chat_context
from app.chat.memory import AgentMemoryAction, AgentMemoryProposal
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.config import Settings
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy
from evals.run_memory_baseline import _SingleAgentEvalDispatcher, _turn_result

_REPO = Path(__file__).resolve().parents[1]
CASES_PATH = _REPO / "evals" / "memory-open-loop-journey-v1.json"
_RESULTS = _REPO / "evals" / "results" / "memory-open-loop-journey-v1"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if set(fixture) != {"version", "cases"} or fixture["version"] != 1:
        raise ValueError("unsupported open-loop journey fixture")
    cases = fixture["cases"]
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("open-loop journey v1 requires two cases")
    seen: set[str] = set()
    for case in cases:
        if set(case) != {"id", "domain", "source", "closure", "query", "expected_state"}:
            raise ValueError("invalid journey case fields")
        if (not isinstance(case["id"], str) or case["id"] in seen
                or case["domain"] not in {"development", "personal"}
                or not case["id"].startswith(
                    "dev_" if case["domain"] == "development" else "personal_"
                ) or any(not isinstance(case[field], str) or not case[field].strip()
                         for field in ("source", "closure", "query"))
                or case["expected_state"] not in {"completed", "cancelled"}):
            raise ValueError("invalid journey case")
        seen.add(case["id"])
    if Counter(case["domain"] for case in cases) != {
        "development": 1, "personal": 1,
    } or Counter(case["expected_state"] for case in cases) != {
        "completed": 1, "cancelled": 1,
    }:
        raise ValueError("journey cases must balance domains and closure actions")
    return cases


async def run_live_case(
    case: dict, adapter: AgentAdapter | None = None, *, timeout_seconds: int = 180,
) -> dict:
    """Use production dispatch; never seed memory directly in the evaluation."""
    with tempfile.TemporaryDirectory(prefix="codecrew-open-loop-journey-") as scratch:
        root = Path(scratch).resolve()
        database = SQLiteDatabase(root / "chat.sqlite3")
        store = StandaloneChatStore(database)
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title=f"Journey eval {case['id']}",
                                   idempotency_key=uuid4())
        workspaces = StandaloneChatWorkspaceManager(root / "workspaces", root / "runtime")
        if adapter is None:
            adapter = KimiCodeAdapter(
                worktree_root=workspaces.workspace_root,
                runtime_root=workspaces.runtime_root,
                policy=PermissionPolicy(allowed_paths=(".",)),
                executable=Settings().kimi_cli_path,
                allow_standalone_chat=True,
            )
        runtime = StandaloneChatAgentRuntime(
            workspaces, {role: adapter for role in (
                MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER,
            )},
        )
        dispatcher = _SingleAgentEvalDispatcher(
            store, runtime, max_turns_per_thread=1, timeout_seconds=timeout_seconds,
        )
        proposals: list[dict] = []
        actions: list[dict] = []
        capture_proposal = store.memory.capture_agent_proposal
        capture_action = store.memory.capture_agent_action

        def observe_proposal(room_id, message_id, proposal: AgentMemoryProposal):
            result = capture_proposal(room_id, message_id, proposal)
            proposals.append({"proposal": proposal.model_dump(mode="json"),
                              "accepted_memory_id": str(result.memory_id) if result else None})
            return result

        def observe_action(room_id, message_id, action: AgentMemoryAction):
            result = capture_action(room_id, message_id, action)
            actions.append({"proposal": action.model_dump(mode="json"),
                            "accepted": result is not None})
            return result

        store.memory.capture_agent_proposal = observe_proposal
        store.memory.capture_agent_action = observe_action
        result = {
            "case_id": case["id"], "domain": case["domain"],
            "expected_state": case["expected_state"],
            "source_turn": None, "closure_turn": None,
            "write_proposals": proposals, "closure_proposals": actions,
            "created": False, "pre_close_recalled": False,
            "closure_accepted": False, "actual_state": None,
            "source_id_preserved": False, "closed_by_closure_message": False,
            "post_restart_recall_absent": None,
            "extra_active_memories": [], "journey_match": False,
            "semantic_review": "pending_human_review",
        }
        await dispatcher.startup()
        try:
            source = service.post_message(
                room.room_id, content=f"@月见 {case['source']}",
                idempotency_key=uuid4(), reply_to=None,
            )
            dispatcher.enqueue(source)
            await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
            result["source_turn"] = _turn_result(store, source)
            result["source_message_id"] = str(source.message.message_id)
            if result["source_turn"]["status"] != "succeeded":
                return result

            created = [memory for memory in store.memory.list_active(room.room_id)
                       if memory.source_message_id == source.message.message_id
                       and memory.kind == "open_loop" and memory.origin == "agent_auto"]
            result["created"] = len(created) == 1
            result["created_memories"] = [memory.model_dump(mode="json")
                                          for memory in created]
            if not result["created"]:
                return result
            memory = created[0]
            closure = service.post_message(
                room.room_id, content=f"@月见 {case['closure']}",
                idempotency_key=uuid4(), reply_to=None,
            )
            result["closure_message_id"] = str(closure.message.message_id)
            result["separate_discussion"] = (
                source.message.correlation_id != closure.message.correlation_id
            )
            before = json.loads(chat_context(store, closure, room))
            result["pre_close_context"] = before["room_memories"]
            result["pre_close_recalled"] = any(
                item["memory_id"] == str(memory.memory_id)
                for item in before["room_memories"]
            )
            dispatcher.enqueue(closure)
            await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
            result["closure_turn"] = _turn_result(store, closure)
        finally:
            await dispatcher.shutdown()

        if result["closure_turn"]["status"] != "succeeded":
            return result
        # A new store object models a process restart, re-reading the same SQLite file.
        restarted = StandaloneChatStore(SQLiteDatabase(root / "chat.sqlite3"))
        restarted.initialize()
        lineage = restarted.memory.lineage(room.room_id, memory.memory_id)
        current = lineage[0][0] if lineage else None
        result["actual_state"] = current.open_loop_state if current else None
        result["source_id_preserved"] = (
            current is not None and current.source_message_id == source.message.message_id
            and restarted.get_message(source.message.message_id).message.message_id
            == source.message.message_id
        )
        result["closed_by_closure_message"] = (
            current is not None and current.closed_by_message_id == closure.message.message_id
        )
        result["closure_accepted"] = any(item["accepted"] for item in actions)
        query = StandaloneChatService(restarted).post_message(
            room.room_id, content=f"@月见 {case['query']}",
            idempotency_key=uuid4(), reply_to=None,
        )
        after = json.loads(chat_context(restarted, query, room))
        result["post_restart_context"] = after["room_memories"]
        result["post_restart_recall_absent"] = all(
            item["memory_id"] != str(memory.memory_id)
            for item in after["room_memories"]
        )
        result["extra_active_memories"] = [
            item.model_dump(mode="json") for item in restarted.memory.list_active(room.room_id)
            if item.memory_id != memory.memory_id
        ]
        result["journey_match"] = all((
            result["created"], result["separate_discussion"],
            result["pre_close_recalled"], result["closure_accepted"],
            result["actual_state"] == case["expected_state"],
            result["source_id_preserved"], result["closed_by_closure_message"],
            result["post_restart_recall_absent"], not result["extra_active_memories"],
        ))
        return result


def summarize(results: list[dict]) -> dict:
    complete = [result for result in results
                if result["source_turn"] and result["source_turn"]["status"] == "succeeded"
                and result["closure_turn"] and result["closure_turn"]["status"] == "succeeded"]
    return {
        "case_count": len(results), "complete_count": len(complete),
        "created_count": sum(result["created"] for result in results),
        "journey_matches": sum(result["journey_match"] for result in complete),
        "semantic_review": "pending_human_review",
    }


async def run_live(cases: list[dict], output_root: Path, timeout_seconds: int) -> Path:
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
        "evals/memory-open-loop-journey-v1.json", "evals/run_memory_open_loop_journey.py",
    )
    manifest = {
        "run_id": run_id, "agent": "kimi-code-cli/implementer",
        "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "selected_cases": [case["id"] for case in cases],
        "code_sha256": {name: hashlib.sha256((_REPO / name).read_bytes()).hexdigest()
                        for name in tracked},
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
        turns = (result["source_turn"], result["closure_turn"])
        if any(turn is not None and turn["status"] != "succeeded" for turn in turns):
            print("Agent turn incomplete; stopping without counting a quality miss.", flush=True)
            break
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--prompt-for-kimi-key", action="store_true")
    parser.add_argument("--case", action="append", help="select a case ID; may repeat")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    cases = load_cases()
    if args.case:
        selected = set(args.case)
        if len(selected) != len(args.case) or not selected <= {case["id"] for case in cases}:
            parser.error("unknown or duplicate journey case")
        cases = [case for case in cases if case["id"] in selected]
    if not args.live:
        print(f"Validated {len(cases)} journey cases; no model called. Use --live for Kimi.")
        return
    if args.timeout_seconds < 1:
        parser.error("timeout must be positive")
    if args.prompt_for_kimi_key:
        os.environ["KIMI_MODEL_API_KEY"] = getpass.getpass("Kimi Code key: ")
    run_dir = asyncio.run(run_live(
        cases, (args.output_root or _RESULTS).resolve(), args.timeout_seconds,
    ))
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
