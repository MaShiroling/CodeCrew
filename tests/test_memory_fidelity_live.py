"""One-turn fidelity replay uses chat dispatch, not the synthetic guard proposal."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from evals.check_memory_fidelity import load_cases, load_live_cases_v2
from evals.run_memory_fidelity_live import run_live_case, summarize


class _ScriptedFakeAdapter(FakeAgentAdapter):
    def __init__(self, reply: dict) -> None:
        super().__init__()
        self._reply = reply

    async def start(self, request):
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(self._reply, ensure_ascii=False),
        })
        return await super().start(request)


@pytest.mark.asyncio
async def test_fidelity_live_replays_real_write_path_with_fake_agent() -> None:
    case = next(item for item in load_cases() if item["id"] == "dev_scope_dropped")
    adapter = _ScriptedFakeAdapter({
        "content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "decision", "content": "我们决定使用 SQLite，仅限本地演示版",
        },
    })
    result = await run_live_case(case, adapter)
    assert result["complete"] is True
    assert result["write_presence_match"] is True
    assert result["kind_match"] is True
    assert result["memories"][0]["content"] == "我们决定使用 SQLite，仅限本地演示版"
    assert result["memories"][0]["source_quote"] is True
    assert result["content_review"] == "pending_human_review"
    assert len(adapter.requests) == 1
    assert summarize([result])["positive_cases_with_write"] == 1


@pytest.mark.asyncio
async def test_fidelity_live_tracks_no_write_negative_case() -> None:
    case = next(item for item in load_cases() if item["id"] == "dev_unresolved_decision")
    adapter = _ScriptedFakeAdapter({
        "content": "还没有确定", "handoff_to": [], "memory_proposal": None,
    })
    result = await run_live_case(case, adapter)
    assert result["complete"] is True
    assert result["memories"] == []
    assert result["write_presence_match"] is True
    assert summarize([result])["negative_cases_without_write"] == 1


@pytest.mark.asyncio
async def test_v2_uses_adjudicated_open_loop_kind() -> None:
    case = next(item for item in load_live_cases_v2()
                if item["id"] == "dev_unresolved_decision")
    adapter = _ScriptedFakeAdapter({
        "content": "先比较方案", "handoff_to": [], "memory_proposal": {
            "kind": "open_loop", "content": "我们还没决定采用 SQLite，先比较两种方案",
        },
    })
    result = await run_live_case(case, adapter)
    assert result["complete"] is True
    assert result["write_presence_match"] is True
    assert result["expected_kind"] == "open_loop"
    assert result["kind_match"] is True
