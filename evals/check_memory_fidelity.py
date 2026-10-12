"""Offline, source-grounded memory write regression; never calls an Agent or API."""

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from uuid import uuid4

from app.chat.memory import AgentMemoryProposal
from app.chat.models import StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.storage import SQLiteDatabase
from app.team.models import MemberRole

CASES_PATH = Path(__file__).resolve().parent / "memory-fidelity-v1.json"
ADJUDICATIONS_PATH = Path(__file__).resolve().parent / "memory-fidelity-v2-adjudications.json"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if set(fixture) != {"version", "cases"} or fixture["version"] != 1:
        raise ValueError("unsupported memory fidelity fixture")
    cases = fixture["cases"]
    if not isinstance(cases, list) or len(cases) != 16:
        raise ValueError("memory fidelity v1 requires 16 cases")
    ids = set()
    for case in cases:
        if set(case) != {
            "id", "domain", "source", "proposal", "live_expected_write", "expected_content",
        }:
            raise ValueError("invalid memory fidelity case fields")
        if (not isinstance(case["id"], str) or case["id"] in ids
                or case["domain"] not in {"development", "personal"}
                or not case["id"].startswith(
                    "dev_" if case["domain"] == "development" else "personal_"
                )):
            raise ValueError("invalid or duplicate memory fidelity case ID")
        ids.add(case["id"])
        if not isinstance(case["source"], str) or not case["source"].strip():
            raise ValueError("memory fidelity source must be nonempty")
        if not isinstance(case["live_expected_write"], bool):
            raise TypeError("live write expectation must be a boolean")
        if (case["expected_content"] is not None
                and (not isinstance(case["expected_content"], str)
                     or not case["expected_content"].strip())):
            raise ValueError("invalid memory fidelity expectation")
        AgentMemoryProposal.model_validate(case["proposal"])
    if Counter(case["domain"] for case in cases) != {
        "development": 8, "personal": 8,
    }:
        raise ValueError("memory fidelity cases must balance both domains")
    if Counter(case["live_expected_write"] for case in cases) != {True: 14, False: 2}:
        raise ValueError("memory fidelity v1 requires 14 positive and 2 negative live cases")
    return cases


def load_live_cases_v2(
    base_path: Path = CASES_PATH, adjudications_path: Path = ADJUDICATIONS_PATH,
) -> list[dict]:
    """Apply explicit user adjudications without editing the frozen v1 labels."""
    cases = load_cases(base_path)
    labels = json.loads(adjudications_path.read_text(encoding="utf-8"))
    if (set(labels) != {"version", "base_fixture_sha256", "adjudications"}
            or labels["version"] != 2
            or labels["base_fixture_sha256"] != hashlib.sha256(base_path.read_bytes()).hexdigest()):
        raise ValueError("memory fidelity v2 adjudications do not match the v1 fixture")
    by_id = {case["id"]: dict(case) for case in cases}
    seen = set()
    for item in labels["adjudications"]:
        if set(item) != {
            "case_id", "live_expected_write", "live_expected_kind", "reason", "decided_at",
        }:
            raise ValueError("invalid memory fidelity adjudication")
        case_id = item["case_id"]
        if case_id not in by_id or case_id in seen:
            raise ValueError("unknown or duplicate memory fidelity adjudication")
        if (not isinstance(item["live_expected_write"], bool)
                or item["live_expected_kind"] not in {
                    "decision", "correction", "preference", "open_loop",
                }
                or not item["reason"].strip() or not item["decided_at"].strip()):
            raise ValueError("invalid memory fidelity adjudication value")
        seen.add(case_id)
        by_id[case_id]["live_expected_write"] = item["live_expected_write"]
        by_id[case_id]["live_expected_kind"] = item["live_expected_kind"]
    return [by_id[case["id"]] for case in cases]


def evaluate_cases(cases: list[dict]) -> list[dict]:
    """Exercise the production persistence edge with an isolated room per case."""
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-fidelity-") as scratch:
        store = StandaloneChatStore(SQLiteDatabase(Path(scratch) / "chat.sqlite3"))
        store.initialize()
        service = StandaloneChatService(store)
        results = []
        for case in cases:
            room = service.create_room(title=case["id"], idempotency_key=uuid4())
            human = next(member for member in room.members if member.role is MemberRole.HUMAN)
            agent = next(member for member in room.members if member.role is MemberRole.PLANNER)
            source = store.append_message(StandaloneChatMessage(
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=human.member_id, recipient_ids=(agent.member_id,),
                content=case["source"], idempotency_key=case["id"],
            ))
            proposal = AgentMemoryProposal.model_validate(case["proposal"])
            memory = store.memory.capture_agent_proposal(
                room.room_id, source.message.message_id, proposal,
            )
            active = store.memory.list_active(room.room_id)
            actual = memory.content if memory else None
            passed = (actual == case["expected_content"]
                      and active == ((memory,) if memory else ())
                      and (memory is None or memory.kind == proposal.kind))
            results.append({
                "id": case["id"], "passed": passed,
                "expected_content": case["expected_content"], "actual_content": actual,
            })
        return results


def main() -> None:
    results = evaluate_cases(load_cases())
    failures = [result for result in results if not result["passed"]]
    for result in failures:
        print(f"FAIL {result['id']}: expected {result['expected_content']!r}, "
              f"got {result['actual_content']!r}")
    print(f"Memory fidelity: {len(results) - len(failures)}/{len(results)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
