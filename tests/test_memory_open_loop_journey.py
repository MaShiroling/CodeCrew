"""The journey evaluator covers Agent write, Agent close, and restart recall."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from evals.run_memory_open_loop_journey import load_cases, run_live_case, summarize


class _JourneyAdapter(FakeAgentAdapter):
    def __init__(self, case: dict, *, omit_write: bool = False) -> None:
        super().__init__()
        self.case = case
        self.omit_write = omit_write

    async def start(self, request):
        line = next(line for line in request.prompt.splitlines()
                    if line.startswith("上下文摘录（JSON）："))
        context = json.loads(line.split("：", 1)[1])
        received = request.prompt.split("收到的消息：", 1)[1].splitlines()[0]
        assert context["scope"] == "none"
        assert context["history"] == []
        if self.case["source"] in received:
            assert context["room_memories"] == []
            payload = {
                "content": "收到", "handoff_to": [], "memory_action": None,
                "memory_proposal": None if self.omit_write else {
                    "kind": "open_loop", "content": self.case["source"],
                },
            }
        else:
            assert self.case["closure"] in received
            memories = context["room_memories"]
            assert len(memories) == 1
            payload = {
                "content": "已处理", "handoff_to": [], "memory_proposal": None,
                "memory_action": {
                    "action": ("complete" if self.case["expected_state"] == "completed"
                               else "cancel"),
                    "memory_id": memories[0]["memory_id"],
                    "evidence": self.case["closure"],
                },
            }
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(payload, ensure_ascii=False),
        })
        return await super().start(request)


def test_journey_fixture_balances_domains_and_states() -> None:
    cases = load_cases()
    assert len(cases) == 2
    assert {case["domain"] for case in cases} == {"development", "personal"}
    assert {case["expected_state"] for case in cases} == {"completed", "cancelled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
async def test_agent_write_close_restart_journey(case: dict) -> None:
    result = await run_live_case(case, _JourneyAdapter(case))
    assert result["source_turn"]["status"] == "succeeded"
    assert result["closure_turn"]["status"] == "succeeded"
    assert result["created"] is True
    assert result["write_proposals"][0]["accepted_memory_id"]
    assert result["pre_close_recalled"] is True
    assert result["closure_accepted"] is True
    assert result["actual_state"] == case["expected_state"]
    assert result["closed_by_closure_message"] is True
    assert result["source_id_preserved"] is True
    assert result["post_restart_recall_absent"] is True
    assert result["extra_active_memories"] == []
    assert result["journey_match"] is True
    assert summarize([result])["journey_matches"] == 1


@pytest.mark.asyncio
async def test_missing_agent_write_is_reported_without_seed_or_closure() -> None:
    case = load_cases()[0]
    result = await run_live_case(case, _JourneyAdapter(case, omit_write=True))
    assert result["source_turn"]["status"] == "succeeded"
    assert result["created"] is False
    assert result["closure_turn"] is None
    assert result["journey_match"] is False
    assert summarize([result])["created_count"] == 0
