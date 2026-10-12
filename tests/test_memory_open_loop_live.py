"""The open-loop Kimi pilot uses isolated production dispatch and explicit labels."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from evals.run_memory_open_loop_live import load_cases, run_live_case, summarize


class _ProposingAdapter(FakeAgentAdapter):
    def __init__(self, action: str | None = None) -> None:
        super().__init__()
        self.action = action

    async def start(self, request):
        line = next(line for line in request.prompt.splitlines()
                    if line.startswith("上下文摘录（JSON）："))
        context = json.loads(line.split("：", 1)[1])
        memories = context["room_memories"]
        payload = {
            "content": "收到，待办和提醒都已关闭", "handoff_to": [], "memory_proposal": None,
            "memory_action": ({
                "action": self.action,
                "memory_id": memories[0]["memory_id"],
                "evidence": request.prompt.split("收到的消息：", 1)[1]
                .splitlines()[0].replace("@月见 ", ""),
            } if self.action else None),
        }
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(payload, ensure_ascii=False),
        })
        return await super().start(request)


def test_open_loop_fixture_is_balanced_and_focuses_on_false_closure() -> None:
    cases = load_cases()
    assert len(cases) == 6
    assert sum(case["domain"] == "development" for case in cases) == 3
    assert sum(state != "open" for case in cases
               for state in case["expected_states"]) == 2


@pytest.mark.asyncio
async def test_live_runner_records_successful_agent_completion() -> None:
    case = next(case for case in load_cases() if case["id"] == "dev_explicit_complete")
    result = await run_live_case(case, _ProposingAdapter("complete"))
    assert result["complete"] is True
    assert result["actual_states"] == ["completed"]
    assert result["closure_proposals"][0]["accepted"] is True
    assert result["false_closures"] == 0
    assert "标记为已完成" in result["turn"]["reply"]
    assert "提醒" not in result["turn"]["reply"]
    assert summarize([result])["state_matches"] == 1


@pytest.mark.asyncio
async def test_live_runner_detects_guard_rejection_on_negation() -> None:
    case = next(case for case in load_cases() if case["id"] == "dev_not_completed")
    result = await run_live_case(case, _ProposingAdapter("complete"))
    assert result["complete"] is True
    assert result["actual_states"] == ["open"]
    assert result["closure_proposals"][0]["accepted"] is False
    assert result["state_match"] is True
    assert "没有修改待办" in result["turn"]["reply"]
    assert "提醒" not in result["turn"]["reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", [
    "dev_not_completed", "dev_conditional_complete", "personal_question_complete",
])
async def test_live_runner_uses_authoritative_open_status_reply(case_id: str) -> None:
    case = next(case for case in load_cases() if case["id"] == case_id)
    result = await run_live_case(case, _ProposingAdapter())
    assert result["complete"] is True
    assert result["state_match"] is True
    assert result["closure_proposals"] == []
    assert "本轮没有修改" in result["turn"]["reply"]
    assert "待办和提醒都已关闭" not in result["turn"]["reply"]
    assert "这项待办已完成" in result["turn"]["reply"]
    assert "这项待办取消了" in result["turn"]["reply"]


@pytest.mark.asyncio
async def test_live_runner_detects_ambiguous_target() -> None:
    case = next(case for case in load_cases() if case["id"] == "personal_ambiguous_target")
    result = await run_live_case(case, _ProposingAdapter("complete"))
    assert result["complete"] is True
    assert result["actual_states"] == ["open", "open"]
    assert result["closure_proposals"][0]["accepted"] is False
    assert "没有修改待办" in result["turn"]["reply"]


@pytest.mark.asyncio
async def test_status_question_with_two_similar_todos_asks_for_exact_target() -> None:
    original = next(case for case in load_cases()
                    if case["id"] == "personal_ambiguous_target")
    case = {**original, "statement": "旅行照片整理完成了吗？"}
    result = await run_live_case(case, _ProposingAdapter())
    assert result["actual_states"] == ["open", "open"]
    assert "多条相似" in result["turn"]["reply"]
    assert "本轮没有修改待办" in result["turn"]["reply"]
