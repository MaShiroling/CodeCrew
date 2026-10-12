"""Answer attribution cases are versioned and prompts protect unknown user stance."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from evals.run_memory_answer_quality import load_cases, load_cases_v2, summarize
from evals.run_memory_fidelity_live import run_live_case


class _ScriptedFakeAdapter(FakeAgentAdapter):
    def __init__(self, reply: dict) -> None:
        super().__init__()
        self._reply = reply

    async def start(self, request):
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(self._reply, ensure_ascii=False),
        })
        return await super().start(request)


def test_answer_quality_cases_are_balanced() -> None:
    cases = load_cases()
    assert len(cases) == 4
    assert sum(case["live_expected_write"] for case in cases) == 2
    assert all(case["expected_answer"] and case["forbidden_inference"] for case in cases)


def test_v2_user_adjudication_preserves_v1_fixture() -> None:
    v1 = {case["id"]: case for case in load_cases()}
    v2 = {case["id"]: case for case in load_cases_v2()}
    case_id = "personal_friend_not_mine"
    assert "以后的默认偏好" in v1[case_id]["forbidden_inference"]
    assert "必须先询问本人" in v2[case_id]["forbidden_inference"]
    assert [key for key in v1 if v1[key]["forbidden_inference"]
            != v2[key]["forbidden_inference"]] == [case_id]


@pytest.mark.asyncio
async def test_answer_prompt_separates_unknown_stance_from_dislike() -> None:
    case = next(item for item in load_cases_v2()
                if item["id"] == "personal_friend_not_mine")
    adapter = _ScriptedFakeAdapter({
        "content": "朋友喜欢香菜；你自己的口味尚未说明。",
        "handoff_to": [], "memory_proposal": None,
    })
    result = await run_live_case(case, adapter)
    assert result["complete"] is True
    assert result["memories"] == []
    assert result["write_presence_match"] is True
    assert "不能推断为‘我不喜欢’" in adapter.requests[0].prompt
    assert "未明确表态时保持未知" in adapter.requests[0].prompt
    assert "建议和举例也不能替 Human 选择" in adapter.requests[0].prompt
    assert "具体操作前必须先问本人" in adapter.requests[0].prompt
    assert "预设不加、不用、另放" in adapter.requests[0].prompt
    assert summarize([result])["answer_quality"] == "pending_semantic_review"
