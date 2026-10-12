"""Offline proof that the memory baseline replays through chat and scores sources."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.agents.models import AgentRole
from app.team.models import MemberRole
from evals.run_memory_baseline import run_case, summarize
from evals.validate_memory_baseline import load_cases
from evals.validate_memory_generalization import validate_frozen
from evals.validate_memory_unseen import validate_unseen


class _ScriptedFakeAdapter(FakeAgentAdapter):
    def __init__(self, replies: list[dict]) -> None:
        super().__init__()
        self._replies = iter(replies)

    async def start(self, request):
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(next(self._replies), ensure_ascii=False),
        })
        return await super().start(request)


class _FakeEmbedder:
    model_fingerprint = "offline-test-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return tuple((1.0, 0.0) for _ in texts)


class _TopicEmbedder:
    model_fingerprint = "offline-topic-test"

    def embed(self, texts):
        return tuple(
            (1.0, 0.0) if any(token in text for token in ("RabbitMQ", "异步作业"))
            else (0.0, 1.0)
            for text in texts
        )


@pytest.mark.asyncio
async def test_current_memory_variant_replays_writes_recall_and_answer() -> None:
    case = next(case for case in load_cases() if case["id"] == "dev_pr_prefix")
    adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [],
         "memory_proposal": {"kind": "decision", "content": "以后所有 PR 标题都要带 feat、fix 等类型前缀"}},
        {"content": "PR 标题要带类型前缀", "handoff_to": [],
         "memory_proposal": None},
    ])
    result = await run_case(case, adapter)
    assert result["complete"] is True
    assert result["actual_write_sources"] == ["m1"]
    assert result["actual_active_sources"] == ["m1"]
    assert result["actual_recall_sources"] == ["m1"]
    assert result["write_source_counts"] == {"tp": 1, "fp": 0, "fn": 0}
    assert result["recall_source_counts"] == {"tp": 1, "fp": 0, "fn": 0}
    assert result["answer_review"]["status"] == "pending_human_review"
    assert len(adapter.requests) == 2
    assert summarize([result])["recall_at_3"] == 1.0


@pytest.mark.asyncio
async def test_hybrid_variant_recalls_paraphrase_missed_by_lexical() -> None:
    case = next(case for case in load_cases() if case["id"] == "personal_chinese_preference")
    replies = [
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "preference", "content": "以后请默认用中文回答我，除非我特别要求英文",
        }},
        {"content": "默认中文", "handoff_to": [], "memory_proposal": None},
    ]
    lexical = await run_case(case, _ScriptedFakeAdapter(replies))
    embedder = _FakeEmbedder()
    hybrid = await run_case(
        case, _ScriptedFakeAdapter(replies), variant="hybrid", embedder=embedder,
    )
    assert lexical["actual_recall_sources"] == []
    assert hybrid["actual_recall_sources"] == ["m1"]
    assert hybrid["variant"] == "hybrid"
    assert len(embedder.calls) >= 2


@pytest.mark.asyncio
async def test_kimi_role_replays_same_case_without_planner_turns() -> None:
    case = next(case for case in load_cases() if case["id"] == "dev_pr_prefix")
    adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [],
         "memory_proposal": {"kind": "decision", "content": "以后所有 PR 标题都要带 feat、fix 等类型前缀"}},
        {"content": "PR 标题要带类型前缀", "handoff_to": [],
         "memory_proposal": None},
    ])
    result = await run_case(case, adapter, agent_role=MemberRole.IMPLEMENTER)
    assert result["complete"] is True
    assert result["agent_role"] == MemberRole.IMPLEMENTER.value
    assert result["actual_write_sources"] == ["m1"]
    assert result["actual_recall_sources"] == ["m1"]
    assert all(request.role is AgentRole.IMPLEMENTER for request in adapter.requests)
    assert all("@月见" in request.prompt for request in adapter.requests)


@pytest.mark.asyncio
async def test_no_memory_control_has_same_turns_but_no_writes_or_recall() -> None:
    case = next(case for case in load_cases() if case["id"] == "dev_pr_prefix")
    adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [],
         "memory_proposal": {"kind": "decision", "content": "以后所有 PR 标题都要带 feat、fix 等类型前缀"}},
        {"content": "我不知道此前约定", "handoff_to": [],
         "memory_proposal": None},
    ])
    result = await run_case(case, adapter, variant="no-memory")
    assert result["complete"] is True
    assert result["actual_write_sources"] == []
    assert result["actual_recall_sources"] == []
    assert result["write_source_counts"] == {"tp": 0, "fp": 0, "fn": 1}
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_all_cases_can_be_replayed_without_live_model() -> None:
    for case in load_cases():
        turn_count = sum(message["role"] == "human" for message in case["messages"]) + 1
        adapter = _ScriptedFakeAdapter([
            {"content": "离线占位回复", "handoff_to": [], "memory_proposal": None}
            for _ in range(turn_count)
        ])
        result = await run_case(case, adapter)
        assert result["complete"], case["id"]
        assert len(adapter.requests) == turn_count, case["id"]


@pytest.mark.asyncio
async def test_replay_preserves_room_and_external_boundaries() -> None:
    cases = {case["id"]: case for case in load_cases()}
    proposal = {"kind": "decision", "content": "A 项目决定用 Ruff 做 Python 静态检查"}
    room_result = await run_case(cases["dev_room_boundary"], _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": proposal},
        {"content": "无法确定 B 项目约定", "handoff_to": [], "memory_proposal": None},
    ]))
    assert room_result["actual_write_sources"] == ["m1"]
    assert room_result["actual_recall_sources"] == []

    external_result = await run_case(cases["dev_external_source"], _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": proposal},
        {"content": "没有本地确认", "handoff_to": [], "memory_proposal": None},
    ]))
    assert external_result["actual_write_sources"] == []
    assert external_result["actual_recall_sources"] == []


@pytest.mark.asyncio
async def test_quoted_friend_baseline_regression_drops_false_positive() -> None:
    case = next(case for case in load_cases() if case["id"] == "personal_quoted_friend")
    adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "correction",
            "content": "冰美式是朋友的偏好，不是用户自己的偏好",
        }},
        {"content": "没有可靠记录", "handoff_to": [], "memory_proposal": None},
    ])
    result = await run_case(case, adapter, agent_role=MemberRole.IMPLEMENTER)
    assert result["complete"] is True
    assert result["actual_write_sources"] == []
    assert result["write_source_counts"] == {"tp": 0, "fp": 0, "fn": 0}


@pytest.mark.asyncio
async def test_room_scoped_format_preference_can_be_saved_without_a_list() -> None:
    case = next(case for case in validate_unseen()
                if case["id"] == "personal_private_room_format")
    adapter = _ScriptedFakeAdapter([
        {"content": "请发清单；本房间的分组方式已记下。", "handoff_to": [],
         "memory_proposal": {"kind": "preference",
                             "content": "在这个私人房间里，请把购物清单按类别分组。"}},
        {"content": "共享房间还没有已确定的版式，请先确认。", "handoff_to": [],
         "memory_proposal": None},
    ])
    result = await run_case(case, adapter, agent_role=MemberRole.IMPLEMENTER)
    assert result["complete"] is True
    assert result["actual_write_sources"] == ["m1"]
    assert result["actual_active_sources"] == ["m1"]
    assert result["actual_recall_sources"] == []
    assert "持续适用的排版、分组" in adapter.requests[0].prompt
    assert "即使本轮尚未给出要处理的清单" in adapter.requests[0].prompt
    assert "本房间偏好扩展到其他房间" in adapter.requests[0].prompt
    assert "在这个私人房间里" not in adapter.requests[1].prompt
    assert "先说明尚未确定" in adapter.requests[1].prompt


@pytest.mark.asyncio
async def test_recalled_decision_prompt_preserves_prohibition_and_status() -> None:
    cases = {case["id"]: case for case in validate_unseen()}
    sql_adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "decision", "content": "只在预发布环境开启详细 SQL 日志，生产环境不要开启。",
        }},
        {"content": "生产环境不要开启详细 SQL 日志。", "handoff_to": [],
         "memory_proposal": None},
    ])
    sql = await run_case(
        cases["dev_sql_log_scope"], sql_adapter, agent_role=MemberRole.IMPLEMENTER,
    )
    assert sql["complete"] is True
    assert sql["actual_recall_sources"] == ["m1"]
    query_prompt = sql_adapter.requests[-1].prompt
    assert "生产环境不要开启" in query_prompt
    assert "不能弱化成‘不建议’" in query_prompt
    assert "不要自行添加例外或临时放行方案" in query_prompt
    assert "不要主动提供与禁令冲突的临时开启" in query_prompt
    assert "先按当前问题明确指向的环境、对象或时间回答能否" in query_prompt
    assert "首句必须明确回答不能" in query_prompt
    assert "在新决定作出前，旧规则仍然有效" in query_prompt

    queue_adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "decision", "content": "后台任务队列决定用 RabbitMQ。",
        }},
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "decision", "content": "文件元信息决定放对象存储，不进任务队列。",
        }},
        {"content": "已记录的选型是 RabbitMQ。", "handoff_to": [],
         "memory_proposal": None},
    ])
    queue = await run_case(
        cases["dev_async_queue_distractor"], queue_adapter,
        variant="hybrid", embedder=_TopicEmbedder(),
        agent_role=MemberRole.IMPLEMENTER,
    )
    assert queue["complete"] is True
    assert queue["actual_recall_sources"] == ["m1"]
    query_prompt = queue_adapter.requests[-1].prompt
    assert "后台任务队列决定用 RabbitMQ" in query_prompt
    assert "‘可能过时’不等于‘尚未决定’" in query_prompt
    assert "不要擅自说成待拍板或要求再次确认" in query_prompt


@pytest.mark.asyncio
async def test_generalization_room_format_is_saved_and_recalled_with_distractor() -> None:
    case = next(case for case in validate_frozen()
                if case["id"] == "personal_family_purchase_layout")

    class PurchaseEmbedder:
        model_fingerprint = "offline-purchase-model"

        def embed(self, texts):
            return tuple((1.0, 0.0) if "采购单" in text else (0.0, 1.0) for text in texts)

    adapter = _ScriptedFakeAdapter([
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "preference", "content": "在家人共享房间里，以后采购单请按购买地点分组。",
        }},
        {"content": "收到", "handoff_to": [], "memory_proposal": {
            "kind": "preference", "content": "以后推荐周末散步路线时请优先平坦路面。",
        }},
        {"content": "按购买地点分组。", "handoff_to": [], "memory_proposal": None},
    ])
    result = await run_case(
        case, adapter, variant="hybrid", embedder=PurchaseEmbedder(),
        agent_role=MemberRole.IMPLEMENTER,
    )
    assert result["complete"]
    assert result["write_source_counts"] == {"tp": 2, "fp": 0, "fn": 0}
    assert result["recall_source_counts"] == {"tp": 1, "fp": 0, "fn": 0}
    assert result["actual_recall_sources"] == ["m1"]
    assert "在家人共享房间里，以后采购单请按购买地点分组。" in adapter.requests[-1].prompt


@pytest.mark.asyncio
async def test_generalization_undecided_status_is_not_written_as_open_loop() -> None:
    case = next(case for case in validate_frozen()
                if case["id"] == "dev_agent_grype_suggestion")
    adapter = _ScriptedFakeAdapter([
        {"content": "可以先比较工具。", "handoff_to": [], "memory_proposal": {
            "kind": "open_loop", "content": "镜像安全扫描工具还没有选定。",
        }},
        {"content": "没有已定结论。", "handoff_to": [], "memory_proposal": None},
    ])
    result = await run_case(case, adapter, agent_role=MemberRole.IMPLEMENTER)
    assert result["complete"]
    assert result["actual_write_sources"] == []
    assert result["actual_recall_sources"] == []
    assert result["write_source_counts"] == {"tp": 0, "fp": 0, "fn": 0}
    assert "不能把你随后建议的比较或评估变成人类交办的任务" in adapter.requests[0].prompt
