"""Replay six synthetic open-loop closures through the real chat dispatcher.

Default mode validates the dataset without a model call. --live uses Kimi Code,
consumes quota, and never invokes the embedding API. A local Human message seeds
each open_loop; one later Human statement receives one Agent turn.
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
from app.chat.memory import AgentMemoryAction, AgentMemoryProposal
from app.chat.models import StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.config import Settings
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy
from evals.run_memory_baseline import _SingleAgentEvalDispatcher, _turn_result

_REPO = Path(__file__).resolve().parents[1]
CASES_PATH = _REPO / "evals" / "memory-open-loop-v1.json"
_RESULTS = _REPO / "evals" / "results" / "memory-open-loop-v1"
_STATES = frozenset({"open", "completed", "cancelled"})


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if set(fixture) != {"version", "cases"} or fixture["version"] != 1:
        raise ValueError("unsupported open-loop fixture")
    cases = fixture["cases"]
    if not isinstance(cases, list) or len(cases) != 6:
        raise ValueError("open-loop v1 requires six cases")
    seen: set[str] = set()
    for case in cases:
        if set(case) != {"id", "domain", "seed", "statement", "expected_states"}:
            raise ValueError("invalid open-loop case fields")
        if (not isinstance(case["id"], str) or case["id"] in seen
                or case["domain"] not in {"development", "personal"}
                or not case["id"].startswith(
                    "dev_" if case["domain"] == "development" else "personal_"
                )):
            raise ValueError("invalid or duplicate open-loop case ID")
        seen.add(case["id"])
        if (not isinstance(case["statement"], str) or not case["statement"].strip()
                or not isinstance(case["seed"], list) or not 1 <= len(case["seed"]) <= 2
                or any(not isinstance(text, str) or not text.strip()
                       for text in case["seed"])):
            raise ValueError("invalid open-loop messages")
        if (not isinstance(case["expected_states"], list)
                or len(case["expected_states"]) != len(case["seed"])
                or any(state not in _STATES for state in case["expected_states"])):
            raise ValueError("invalid open-loop expectations")
    if Counter(case["domain"] for case in cases) != {"development": 3, "personal": 3}:
        raise ValueError("open-loop cases must balance development and personal")
    if sum(state != "open" for case in cases for state in case["expected_states"]) != 2:
        raise ValueError("open-loop v1 requires two positive closures")
    return cases


async def run_live_case(
    case: dict, adapter: AgentAdapter | None = None, *, timeout_seconds: int = 180,
) -> dict:
    """Exercise production context, Agent parsing, server guard, and persistence."""
    with tempfile.TemporaryDirectory(prefix="codecrew-open-loop-live-") as scratch:
        root = Path(scratch).resolve()
        store = StandaloneChatStore(SQLiteDatabase(root / "chat.sqlite3"))
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title=f"Open-loop eval {case['id']}", idempotency_key=uuid4())
        human = next(member for member in room.members if member.role is MemberRole.HUMAN)
        agent = next(member for member in room.members if member.role is MemberRole.IMPLEMENTER)
        seeded = []
        for index, text in enumerate(case["seed"]):
            source = store.append_message(StandaloneChatMessage(
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=human.member_id, recipient_ids=(agent.member_id,),
                content=f"@月见 {text}",
                idempotency_key=f"open-loop-eval:{case['id']}:seed:{index}",
            ))
            memory = store.memory.capture_agent_proposal(
                room.room_id, source.message.message_id,
                AgentMemoryProposal(kind="open_loop", content=text),
            )
            if memory is None:
                raise ValueError(f"failed to seed open_loop: {case['id']}:{index}")
            seeded.append(memory)
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
        capture = store.memory.capture_agent_action

        def observe_action(room_id, message_id, action: AgentMemoryAction):
            record = {"proposal": action.model_dump(mode="json"), "accepted": False}
            proposals.append(record)
            result = capture(room_id, message_id, action)
            record["accepted"] = result is not None
            return result

        store.memory.capture_agent_action = observe_action
        await dispatcher.startup()
        try:
            statement = store.append_message(StandaloneChatMessage(
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=human.member_id, recipient_ids=(agent.member_id,),
                content=f"@月见 {case['statement']}",
                idempotency_key=f"open-loop-eval:{case['id']}:statement",
            ))
            dispatcher.enqueue(statement)
            await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
            turn = _turn_result(store, statement)
            all_turns = store.list_turns(room.room_id)
            complete = (turn["status"] == "succeeded" and len(all_turns) == 1
                        and all_turns[0].recipient_id == agent.member_id)
            actual = [store.memory.lineage(room.room_id, memory.memory_id)[0][0]
                      for memory in seeded]
            states = [memory.open_loop_state for memory in actual]
            extra = [memory.model_dump(mode="json") for memory in
                     store.memory.list_active(room.room_id)
                     if memory.memory_id not in {item.memory_id for item in seeded}]
            return {
                "case_id": case["id"], "domain": case["domain"],
                "complete": complete, "statement": statement.message.content,
                "seed": [{"memory_id": str(memory.memory_id), "content": memory.content}
                         for memory in seeded],
                "expected_states": case["expected_states"], "actual_states": states,
                "closure_proposals": proposals, "turn": turn,
                "state_match": states == case["expected_states"] if complete else None,
                "false_closures": sum(expected == "open" and actual != "open"
                                      for expected, actual in zip(
                                          case["expected_states"], states, strict=True)),
                "extra_active_memories": extra,
                "semantic_review": "pending_human_review",
            }
        finally:
            await dispatcher.shutdown()


def summarize(results: list[dict]) -> dict:
    complete = [result for result in results if result["complete"]]
    return {
        "case_count": len(results), "complete_count": len(complete),
        "state_matches": sum(result["state_match"] is True for result in complete),
        "false_closures": sum(result["false_closures"] for result in complete),
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
        "evals/memory-open-loop-v1.json", "evals/run_memory_open_loop_live.py",
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
        if not result["complete"]:
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
        if len(selected) != len(args.case) or not selected <= {
            case["id"] for case in cases
        }:
            parser.error("unknown or duplicate open-loop case")
        cases = [case for case in cases if case["id"] in selected]
    if not args.live:
        print(f"Validated {len(cases)} open-loop cases; no model called. Use --live to invoke Kimi.")
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
