"""Sanitized real failure replay on temporary stores; no paid or remote calls."""

import copy
import json
import urllib.request
from uuid import uuid4

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.dispatch import _closed_open_loop_status_receipt
from app.chat.memory import AgentMemoryProposal
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import OpenAICompatibleEmbedder
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from evals import memory_persistent_resume as resume
from evals import run_memory_persistent_live as probe
from tests.test_memory_persistent_live import Adapter, journey_fixture
from tests.test_memory_persistent_resume import prefix_fixture, validate
from tests.test_standalone_chat_dispatch import setup

ISOLATION_QUERY = probe.STEPS[11][2]
BAD_CLOSED_REPLY = (
    "这个房间里我目前没有看到任何已记录的待办清单——关于 dentist checkup 牙科检查，"
    "我这里没有留下它是待办、已完成还是已取消的线索。所以既不能说它还在待办里，"
    "也不能说它已经被勾掉了。"
)


@pytest.fixture(autouse=True)
def no_paid_or_remote(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("paid embedding"))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **k: pytest.fail("actual process"))


def store_fixture(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="regression", idempotency_key=uuid4())
    return store, service, room


def post(service, room, content):
    return service.post_message(room.room_id, content="@月见 " + content, idempotency_key=uuid4(), reply_to=None)


@pytest.mark.parametrize("source,quote", [
    (ISOLATION_QUERY, "没有记录就说明未知"),
    (ISOLATION_QUERY, ISOLATION_QUERY),
    ("这次回答默认简短。", "这次回答默认简短。"),
    ("这次回答，默认简短。", "默认简短。"),
    ("本轮请按类别排购物清单。", "本轮请按类别排购物清单。"),
    ("这条回复默认中文。", "这条回复默认中文。"),
    ("这杯咖啡我喜欢无糖。", "这杯咖啡我喜欢无糖。"),
    ("以后默认中文吗？", "以后默认中文"),
    ("我喜欢拿铁？", "我喜欢拿铁"),
    ("我喜欢拿铁吗。", "我喜欢拿铁"),
    ("是否以后默认中文。", "是否以后默认中文。"),
    ("我还没决定以后默认哪种语言。", "我还没决定以后默认哪种语言。"),
    ("请按类别排购物清单。", "请按类别排购物清单。"),
])
def test_one_shot_or_question_preference_rejected_atomically(tmp_path, source, quote):
    store, service, room = store_fixture(tmp_path)
    message = post(service, room, source)
    assert store.memory.capture_agent_proposal(room.room_id, message.message.message_id,
        AgentMemoryProposal(kind="preference", content=quote)) is None
    assert store.memory.list_active(room.room_id) == ()
    with store.database.connect() as conn:
        for table in ("standalone_chat_memories", "standalone_chat_memory_candidates",
                      "standalone_chat_memory_embeddings", "standalone_chat_vector_outbox"):
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


@pytest.mark.parametrize("source,quote", [
    ("以后没有记录就说明未知。", "以后没有记录就说明未知。"),
    ("以后回答默认简短。", "以后回答默认简短。"),
    ("今后采购单请按购买地点分组。", "今后采购单请按购买地点分组。"),
    ("我喜欢拿铁。", "我喜欢拿铁。"),
    ("我不喜欢香菜。", "我不喜欢香菜。"),
    ("我只在周末安排晨跑，工作日不要给我排晨跑。", "我只在周末安排晨跑，工作日不要给我排晨跑。"),
    ("只有周末点外卖时我才接受辣味，工作日请选不辣。", "只有周末点外卖时我才接受辣味，工作日请选不辣。"),
    ("在这个私人房间里，请把购物清单按类别分组。", "在这个私人房间里，请把购物清单按类别分组。"),
    ("本房间请简短回答。", "本房间请简短回答。"),
    ("在家人共享房间里，以后采购单请按购买地点分组。", "在家人共享房间里，以后采购单请按购买地点分组。"),
    ("网关超时是多少？我喜欢拿铁。", "我喜欢拿铁。"),
    ("这次请简短回答。以后默认中文。", "以后默认中文。"),
    ("以后默认中文。网关超时是多少？", "以后默认中文。"),
    ("我朋友喜欢冰美式，但我喜欢拿铁。", "我喜欢拿铁"),
])
def test_stable_or_independent_mixed_message_preference_kept(tmp_path, source, quote):
    store, service, room = store_fixture(tmp_path)
    message = post(service, room, source)
    memory = store.memory.capture_agent_proposal(room.room_id, message.message.message_id,
        AgentMemoryProposal(kind="preference", content=quote))
    assert memory is not None and memory.content == quote


@pytest.mark.parametrize("query", [
    "dentist checkup 牙科检查还在未完成待办里吗？",
    "dentist checkup 牙科检查还在未完成的待办清单里面吗？",
    "dentist checkup 牙科检查还在未关闭待办中吗？",
    "dentist checkup 牙科检查还在待办列表里吗？",
])
@pytest.mark.parametrize("action,state", [("complete", "已完成"), ("cancel", "已取消")])
@pytest.mark.asyncio
async def test_closed_membership_variants_use_sqlite_after_restart_without_agent(tmp_path, query, action, state):
    adapter = FakeAgentAdapter(FakeAgentScenario(output={"message": json.dumps({
        "content": BAD_CLOSED_REPLY, "handoff_to": [], "memory_proposal": None,
    }, ensure_ascii=False)}))
    service, dispatcher, _ = setup(tmp_path, implementer=adapter)
    room = service.create_room(title="state", idempotency_key=uuid4())
    source = post(service, room, "待办：预约 dentist checkup 牙科检查。")
    memory = service.store.memory.capture_agent_proposal(room.room_id, source.message.message_id,
        AgentMemoryProposal(kind="open_loop", content="待办：预约 dentist checkup 牙科检查。"))
    assert memory is not None
    service.store.memory.close_open_loop(room.room_id, memory.memory_id, action=action)
    # Replace the store with a fresh instance on the same test-owned file.
    restarted = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    restarted.initialize()
    dispatcher.store = restarted
    service = StandaloneChatService(restarted)
    await dispatcher.startup()
    try:
        message = post(service, room, query)
        dispatcher.enqueue(message)
        await dispatcher.wait_idle()
        assert adapter.requests == []
        reply = restarted.list_messages(room.room_id)[-1].message.content
        assert f"在记忆记录中标记为{state}" in reply
        assert "不再属于未完成待办" in reply and "没有留下" not in reply
        assert restarted.memory.retrieve(room.room_id, "dentist checkup 牙科检查") == ()
        assert restarted.list_turns(room.room_id)[0].session_id is None
    finally:
        await dispatcher.shutdown()


@pytest.mark.parametrize("query", [
    "dentist checkup 牙科检查还在未完成待办里。",
    "dentist checkup 牙科检查还在未完成待办里吗？怎么取消？",
    "dentist checkup 牙科检查还在未完成待办里吗？是否要取消？",
    "如果 dentist checkup 牙科检查取消了，还在未完成待办里吗？",
    "其他预约还在未完成待办里吗？",
])
def test_closed_lookup_does_not_expand_to_declarations_procedure_or_condition(tmp_path, query):
    store, service, room = store_fixture(tmp_path)
    source = post(service, room, "待办：预约 dentist checkup 牙科检查。")
    memory = store.memory.capture_agent_proposal(room.room_id, source.message.message_id,
        AgentMemoryProposal(kind="open_loop", content="待办：预约 dentist checkup 牙科检查。"))
    store.memory.close_open_loop(room.room_id, memory.memory_id, action="cancel")
    message = post(service, room, query)
    assert _closed_open_loop_status_receipt(store, room, message) is None


def test_closed_membership_matching_still_refuses_ambiguous_targets(tmp_path):
    store, service, room = store_fixture(tmp_path)
    for content in ("待办：预约 dentist checkup 牙科检查。", "待办：dentist checkup 牙科检查带上牙片。"):
        source = post(service, room, content)
        memory = store.memory.capture_agent_proposal(room.room_id, source.message.message_id,
            AgentMemoryProposal(kind="open_loop", content=content))
        if "预约" in content:
            store.memory.close_open_loop(room.room_id, memory.memory_id, action="cancel")
    message = post(service, room, probe.STEPS[10][2])
    receipt = _closed_open_loop_status_receipt(store, room, message)
    assert "多条相似待办" in receipt and "无法确定" in receipt


@pytest.mark.parametrize("reply", [BAD_CLOSED_REPLY, "已取消", "可能已取消。", "还没有取消。", "如果已取消就不在待办里。"])
def test_keyword_is_not_closed_state_evidence(tmp_path, reply):
    store, service, room = store_fixture(tmp_path)
    source = post(service, room, "待办：预约 dentist checkup 牙科检查。")
    memory = store.memory.capture_agent_proposal(room.room_id, source.message.message_id,
        AgentMemoryProposal(kind="open_loop", content="待办：预约 dentist checkup 牙科检查。"))
    closed = store.memory.close_open_loop(room.room_id, memory.memory_id, action="cancel")
    assert "取消" in reply  # This was the insufficient V2 gate.
    with pytest.raises(resume.ResumeError, match="closed_state_receipt_missing"):
        resume.hybrid_query_check("closed_personal_query", reply, [], {"create_personal_todo": closed})


class ActualFailureReplayAdapter(Adapter):
    async def start(self, request):
        received = request.prompt.split("收到的消息：", 1)[1].splitlines()[0]
        if ISOLATION_QUERY in received:
            self.starts += 1
            self._scenario = FakeAgentScenario(output={"message": json.dumps({
                "content": "尚未确定，本房间没有 API 网关读超时记录。", "handoff_to": [],
                "memory_proposal": {"kind": "preference", "content": "没有记录就说明未知"},
            }, ensure_ascii=False)})
            return await FakeAgentAdapter.start(self, request)
        return await super().start(request)


@pytest.mark.asyncio
async def test_real_spurious_proposal_replay_full_production_journey(tmp_path):
    journey, remote = journey_fixture(tmp_path)
    adapter = ActualFailureReplayAdapter()
    journey.adapter = adapter
    journey.dispatcher.runtime.adapters = {r: adapter for r in (
        MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)}
    assert await journey.run()
    entry = journey.steps[10]
    assert entry["closed_state_evidence"]["agent_started"] is False
    assert entry["closed_state_evidence"]["source"] == "sqlite_status_receipt"
    assert "标记为已取消" in entry["turn"]["reply"]
    assert journey.proposals[-1]["proposal"]["content"] == "没有记录就说明未知"
    assert journey.proposals[-1]["accepted"] is None
    assert journey.steps[-1]["passed"] and adapter.starts == 11
    assert not journey.store.memory.list_active(journey.rooms["personal"])
    assert len(remote.points) == 4
    assert not any(journey.worker.status().get(k, 0) for k in ("pending", "processing", "waiting", "dead"))
    assert probe.finish_audit(journey.worker)["consistent_at_observation"]


@pytest.mark.asyncio
async def test_v3_resume_checks_closed_receipt_origin_not_only_text(tmp_path, monkeypatch):
    journey, _, path, report = await prefix_fixture(tmp_path, monkeypatch, count=11)
    assert validate(journey, path).summary()["next_step"] == 12
    changed = copy.deepcopy(report)
    changed["steps"][-1]["closed_state_evidence"]["agent_started"] = True
    path.write_text(json.dumps(changed))
    with pytest.raises(resume.ResumeError, match="closed_status_evidence_changed"):
        validate(journey, path)
