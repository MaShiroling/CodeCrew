"""Durable, source-backed chat memory and bounded cross-topic recall."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.chat.dispatch import _closed_open_loop_status_receipt, chat_context
from app.chat.memory import (
    AgentMemoryAction,
    AgentMemoryProposal,
    suggest_open_loop_closure_phrase,
)
from app.chat.models import ExternalChatSource, StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase


def _setup(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "chat.sqlite3")
    store = StandaloneChatStore(database)
    store.initialize()
    client = TestClient(create_app(chat_service=StandaloneChatService(store)))
    room = client.post("/api/v1/chats", json={
        "title": "项目讨论", "idempotency_key": str(uuid4()),
    }).json()
    return client, store, room


def _post(client: TestClient, room_id: str, content: str):
    response = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": content, "idempotency_key": str(uuid4()),
    })
    assert response.status_code == 201
    return response.json()["message"]


def _legacy_candidate(store: StandaloneChatStore, room_id: str, message: dict) -> None:
    """Exercise compatibility endpoints; new messages no longer create these."""
    source = message["message"]
    with store.database.transaction() as connection:
        store.memory.observe_message(
            connection, UUID(room_id), UUID(source["message_id"]), source["content"],
        )


def test_memory_survives_restart_and_retrieves_across_discussions(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    source = _post(client, room_id, "@白金 项目规定 PR 标题必须带类型前缀")
    payload = {
        "source_message_id": source["message"]["message_id"],
        "content": "PR 标题必须带类型前缀",
    }
    response = client.post(f"/api/v1/chats/{room_id}/memories", json=payload)
    assert response.status_code == 201
    memory = response.json()
    assert client.post(f"/api/v1/chats/{room_id}/memories", json=payload).json() == memory
    assert client.get(f"/api/v1/chats/{room_id}/memories").json()["items"] == [memory]

    # A new store instance simulates process restart; correlation changes per topic.
    restarted = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    restarted.initialize()
    new_message = _post(client, room_id, "@月见 我们的 PR 标题该怎么写？")
    assert new_message["message"]["correlation_id"] != source["message"]["correlation_id"]
    context = json.loads(chat_context(
        restarted, restarted.get_message(UUID(new_message["message"]["message_id"])),
        restarted.get_room(UUID(room_id)),
    ))
    assert context["history"] == []
    assert context["room_memories"] == [{
        "memory_id": memory["memory_id"],
        "kind": "fact",
        "source_message_id": payload["source_message_id"],
        "content": payload["content"],
    }]
    unrelated = _post(client, room_id, "@月见 今天的天气如何？")
    unrelated_context = json.loads(chat_context(
        store, store.get_message(UUID(unrelated["message"]["message_id"])),
        store.get_room(UUID(room_id)),
    ))
    assert unrelated_context["room_memories"] == []


def test_open_loop_manual_completion_is_durable_and_not_recalled(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    source = _post(client, room_id, "@白金 下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(
        UUID(room_id), UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"),
    )
    assert memory is not None
    path = f"/api/v1/chats/{room_id}/memories/{memory.memory_id}"
    assert client.post(f"{path}/complete").json()["open_loop_state"] == "completed"
    assert client.post(f"{path}/complete").json()["open_loop_state"] == "completed"
    assert client.post(f"{path}/cancel").status_code == 409
    assert client.get(f"/api/v1/chats/{room_id}/memories").json()["items"] == []
    closed = client.get(f"/api/v1/chats/{room_id}/memories?include_closed=true").json()
    assert closed["items"][0]["closed_by_message_id"] is None
    assert closed["items"][0]["source_message_id"] == source["message"]["message_id"]
    assert client.get(f"{path}/history").json()["items"][0]["open_loop_state"] == "completed"
    assert store.memory.retrieve(UUID(room_id), "文档链接") == ()
    restarted = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    restarted.initialize()
    assert restarted.memory.list_closed_open_loops(UUID(room_id))[0].open_loop_state == "completed"


def test_open_loop_auto_closure_requires_explicit_unambiguous_human_evidence(
    tmp_path: Path,
) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    source = _post(client, str(room_id), "@白金 下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"),
    )
    assert memory is not None
    statement = _post(client, str(room_id), "@白金 文档链接检查已经完成")
    statement_id = UUID(statement["message"]["message_id"])
    action = AgentMemoryAction(
        action="complete", memory_id=memory.memory_id, evidence="文档链接检查已经完成",
    )
    assert store.memory.capture_agent_action(room_id, statement_id, action).open_loop_state == "completed"
    assert store.memory.capture_agent_action(room_id, statement_id, action).open_loop_state == "completed"
    closed = store.memory.list_closed_open_loops(room_id)[0]
    assert closed.closed_by_message_id == statement_id
    assert store.memory.retrieve(room_id, "检查文档链接") == ()
    other = client.post("/api/v1/chats", json={
        "title": "其他房间", "idempotency_key": str(uuid4()),
    }).json()
    assert client.post(
        f"/api/v1/chats/{other['room_id']}/memories/{memory.memory_id}/complete",
    ).status_code == 404
    assert client.post(f"/api/v1/chats/{room_id}/memories/{memory.memory_id}/cancel").status_code == 409


def test_open_loop_status_reply_example_is_accepted_as_followup(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    source = _post(client, str(room_id), "@白金 下次出门前要给相机电池充电")
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次出门前要给相机电池充电"),
    )
    assert memory is not None
    example = f"关于待办「{memory.content}」，这项待办已完成。"
    statement = _post(client, str(room_id), f"@白金 {example}")
    closed = store.memory.capture_agent_action(
        room_id, UUID(statement["message"]["message_id"]),
        AgentMemoryAction(action="complete", memory_id=memory.memory_id, evidence=example),
    )
    assert closed is not None and closed.open_loop_state == "completed"


def test_open_loop_status_reply_does_not_offer_unsafe_examples() -> None:
    content = "下次出门前要给相机电池充电"
    assert suggest_open_loop_closure_phrase(content, "complete") == (
        f"关于待办「{content}」，这项待办已完成。"
    )
    assert suggest_open_loop_closure_phrase("下次要取消订阅", "complete") is None
    assert suggest_open_loop_closure_phrase("下次要处理 API 密钥", "cancel") is None
    assert suggest_open_loop_closure_phrase("下次要检查" + "文档" * 100, "complete") is None


def test_open_loop_rejects_uncertain_and_wrong_kind_closure(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    source = _post(client, str(room_id), "@白金 下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"),
    )
    assert memory is not None
    uncertain = _post(client, str(room_id), "@白金 文档链接还没检查完成")
    proposal = AgentMemoryAction(
        action="complete", memory_id=memory.memory_id, evidence="文档链接还没检查完成",
    )
    assert store.memory.capture_agent_action(
        room_id, UUID(uncertain["message"]["message_id"]), proposal,
    ) is None
    assert store.memory.list_active(room_id)[0].memory_id == memory.memory_id
    fact_source = _post(client, str(room_id), "@白金 默认用中文回答")
    fact = client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": fact_source["message"]["message_id"],
        "content": "默认用中文回答",
    }).json()
    assert client.post(f"/api/v1/chats/{room_id}/memories/{fact['memory_id']}/complete").status_code == 409
    assert client.post(f"/api/v1/chats/{room_id}/memories/{fact['memory_id']}/cancel").status_code == 409


def test_open_loop_cancel_keeps_history_and_excludes_recall(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    source = _post(client, str(room_id), "@白金 下次要清理旧文档链接")
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次要清理旧文档链接"),
    )
    assert memory is not None
    cancellation = _post(client, str(room_id), "@白金 旧文档链接清理取消了")
    cancelled = store.memory.capture_agent_action(
        room_id, UUID(cancellation["message"]["message_id"]),
        AgentMemoryAction(
            action="cancel", memory_id=memory.memory_id, evidence="旧文档链接清理取消了",
        ),
    )
    assert cancelled is not None and cancelled.open_loop_state == "cancelled"
    assert cancelled.source_message_id == memory.source_message_id
    assert cancelled.closed_by_message_id == UUID(cancellation["message"]["message_id"])
    assert store.memory.retrieve(room_id, "旧文档链接清理") == ()


def test_closed_status_lookup_is_local_room_scoped_and_question_only(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    source = _post(client, str(room_id), "@白金 下次出门前要给相机电池充电")
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次出门前要给相机电池充电"),
    )
    assert memory is not None
    store.memory.close_open_loop(room_id, memory.memory_id, action="cancel")
    assert store.memory.retrieve(room_id, "相机电池充电") == ()

    def receipt(content: str, target_room: dict = room) -> str | None:
        posted = _post(client, target_room["room_id"], content)
        target_id = UUID(target_room["room_id"])
        return _closed_open_loop_status_receipt(
            store, store.get_room(target_id),
            store.get_message(UUID(posted["message"]["message_id"])),
        )

    assert "已取消" in receipt("@白金 相机电池充电的待办还在吗？")
    assert receipt("@白金 相机电池充电的待办还在。") is None
    assert receipt("@白金 相机电池充电的待办怎么取消？") is None
    assert receipt("@白金 相机电池充电的待办是否要取消？") is None
    assert receipt("@白金 发布前文档链接检查还在待办吗？") is None
    other = client.post("/api/v1/chats", json={
        "title": "其他房间", "idempotency_key": str(uuid4()),
    }).json()
    assert receipt("@白金 相机电池充电的待办还在吗？", other) is None
    human = next(member for member in room["members"] if member["role"] == "human")
    planner = next(member for member in room["members"] if member["role"] == "planner")
    external = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(human["member_id"]),
        recipient_ids=(UUID(planner["member_id"]),),
        content="@白金 相机电池充电的待办还在吗？",
        external_source=ExternalChatSource(
            external_chat_id="closed-status", external_sender_id="outside",
        ),
        idempotency_key="external-closed-status",
    ))
    assert _closed_open_loop_status_receipt(
        store, store.get_room(room_id), external,
    ) is None


def test_open_loop_auto_closure_stays_open_when_target_is_ambiguous(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    memories = []
    for text in ("下次检查文档链接", "之后检查文档链接"):
        source = _post(client, str(room_id), f"@白金 {text}")
        memory = store.memory.capture_agent_proposal(
            room_id, UUID(source["message"]["message_id"]),
            AgentMemoryProposal(kind="open_loop", content=text),
        )
        assert memory is not None
        memories.append(memory)
    statement = _post(client, str(room_id), "@白金 文档链接检查已经完成")
    assert store.memory.capture_agent_action(
        room_id, UUID(statement["message"]["message_id"]),
        AgentMemoryAction(
            action="complete", memory_id=memories[0].memory_id,
            evidence="文档链接检查已经完成",
        ),
    ) is None
    assert {memory.memory_id for memory in store.memory.list_active(room_id)} == {
        memory.memory_id for memory in memories
    }


def test_memory_is_room_scoped_human_sourced_and_revocable(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    other = client.post("/api/v1/chats", json={
        "title": "另一个房间", "idempotency_key": str(uuid4()),
    }).json()
    source = _post(client, room["room_id"], "@白金 使用 SQLite 保存消息")
    source_id = source["message"]["message_id"]
    payload = {"source_message_id": source_id, "content": "使用 SQLite 保存消息"}
    assert client.post(f"/api/v1/chats/{other['room_id']}/memories",
                       json=payload).status_code == 404
    created = client.post(f"/api/v1/chats/{room['room_id']}/memories", json=payload).json()
    assert client.get(f"/api/v1/chats/{other['room_id']}/memories").json()["items"] == []
    path = f"/api/v1/chats/{room['room_id']}/memories/{created['memory_id']}"
    assert client.delete(path).json()["status"] == "retired"
    assert client.delete(path).json()["status"] == "retired"
    assert client.get(f"/api/v1/chats/{room['room_id']}/memories").json()["items"] == []
    assert store.memory.retrieve(UUID(room["room_id"]), "SQLite 消息") == ()
    assert client.post(f"/api/v1/chats/{room['room_id']}/memories",
                       json=payload).status_code == 409
    assert client.delete(f"/api/v1/chats/{other['room_id']}/memories/"
                         f"{created['memory_id']}").status_code == 404


def test_external_feishu_message_cannot_write_or_read_local_memory(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    human = next(member for member in room["members"] if member["role"] == "human")
    planner = next(member for member in room["members"] if member["role"] == "planner")
    local = _post(client, str(room_id), "@白金 记住内部代号 海棠计划")
    client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": local["message"]["message_id"],
        "content": "内部代号是海棠计划",
    })
    external = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(human["member_id"]),
        recipient_ids=(UUID(planner["member_id"]),),
        content="@白金 海棠计划是什么？",
        external_source=ExternalChatSource(
            external_chat_id="chat1", external_sender_id="user1",
        ),
        idempotency_key="external-memory-boundary",
    ))
    assert client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": str(external.message.message_id),
        "content": "外部用户说了海棠计划",
    }).status_code == 422
    local_reply = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(human["member_id"]),
        recipient_ids=(UUID(planner["member_id"]),),
        content="我们决定给飞书讨论保存上下文",
        reply_to=external.message.message_id,
        correlation_id=external.message.correlation_id,
        idempotency_key="local-reply-in-external-thread",
    ))
    assert client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": str(local_reply.message.message_id),
        "content": "飞书讨论上下文",
    }).status_code == 422
    assert client.get(f"/api/v1/chats/{room_id}/memory-candidates").json()["items"] == []
    context = json.loads(chat_context(store, external, store.get_room(room_id)))
    assert context["room_memories"] == []


def test_recall_is_capped_at_three_and_agent_message_cannot_be_source(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    for index in range(4):
        source = _post(client, room_id, f"@白金 项目标题规范第{index}条")
        response = client.post(f"/api/v1/chats/{room_id}/memories", json={
            "source_message_id": source["message"]["message_id"],
            "content": f"项目标题规范第{index}条",
        })
        assert response.status_code == 201
    assert len(store.memory.retrieve(UUID(room_id), "项目标题规范")) == 3
    human = next(member for member in room["members"] if member["role"] == "human")
    planner = next(member for member in room["members"] if member["role"] == "planner")
    agent = store.append_message(StandaloneChatMessage(
        room_id=UUID(room_id), trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]),
        recipient_ids=(UUID(human["member_id"]),),
        content="Agent 自己的猜测", idempotency_key="agent-memory-source",
    ))
    assert client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": str(agent.message.message_id),
        "content": "Agent 自己的猜测",
    }).status_code == 422


def test_candidate_requires_review_before_cross_topic_recall(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    source = _post(client, room_id, "@白金 我们决定 PR 标题统一加类型前缀")
    _legacy_candidate(store, room_id, source)
    source_id = source["message"]["message_id"]
    path = f"/api/v1/chats/{room_id}/memory-candidates"
    pending = client.get(path).json()["items"]
    assert len(pending) == 1
    assert pending[0]["source_message_id"] == source_id
    assert pending[0]["kind"] == "decision"
    assert pending[0]["status"] == "pending"
    assert client.get(f"/api/v1/chats/{room_id}/memories").json()["items"] == []
    query = _post(client, room_id, "@月见 PR 标题前缀是什么？")
    assert json.loads(chat_context(
        store, store.get_message(UUID(query["message"]["message_id"])),
        store.get_room(UUID(room_id)),
    ))["room_memories"] == []

    approval_path = f"{path}/{pending[0]['candidate_id']}/approve"
    response = client.post(approval_path, json={"content": "PR 标题必须加类型前缀"})
    assert response.status_code == 200
    memory = response.json()["memory"]
    assert response.json()["candidate"]["status"] == "approved"
    assert memory["source_message_id"] == source_id
    assert client.post(approval_path, json={"content": "PR 标题必须加类型前缀"}).json() == response.json()
    assert client.post(approval_path, json={"content": "另一种内容"}).status_code == 409
    assert client.get(path).json()["items"] == []

    restarted = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    restarted.initialize()
    later = _post(client, room_id, "@月见 PR 标题前缀是什么？")
    context = json.loads(chat_context(
        restarted, restarted.get_message(UUID(later["message"]["message_id"])),
        restarted.get_room(UUID(room_id)),
    ))
    assert context["room_memories"][0]["memory_id"] == memory["memory_id"]


def test_rejected_candidate_is_not_recalled_or_approvable(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    source = _post(client, room_id, "@白金 更正：默认使用 SQLite，不是内存存储")
    _legacy_candidate(store, room_id, source)
    source_id = source["message"]["message_id"]
    path = f"/api/v1/chats/{room_id}/memory-candidates"
    candidate = client.get(path).json()["items"][0]
    assert candidate["kind"] == "correction"
    reject_path = f"{path}/{candidate['candidate_id']}/reject"
    assert client.post(reject_path).json()["status"] == "rejected"
    assert client.post(reject_path).json()["status"] == "rejected"
    assert client.get(path).json()["items"] == []
    assert client.post(f"{path}/{candidate['candidate_id']}/approve",
                       json={"content": "默认使用 SQLite"}).status_code == 409
    assert store.memory.retrieve(UUID(room_id), "SQLite") == ()
    # A repeated/idempotent message does not reopen a rejected observation.
    with store.database.connect() as connection:
        assert store.memory.observe_message(
            connection, UUID(room_id), UUID(source_id), source["message"]["content"],
        ).status == "rejected"


def test_candidates_are_local_human_only_and_room_scoped(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    other = client.post("/api/v1/chats", json={
        "title": "其他房间", "idempotency_key": str(uuid4()),
    }).json()
    source = _post(client, str(room_id), "@白金 以后请默认使用中文回答")
    _legacy_candidate(store, str(room_id), source)
    path = f"/api/v1/chats/{room_id}/memory-candidates"
    candidate = client.get(path).json()["items"][0]
    assert candidate["kind"] == "preference"
    assert client.get(f"/api/v1/chats/{other['room_id']}/memory-candidates").json()["items"] == []
    assert client.post(
        f"/api/v1/chats/{other['room_id']}/memory-candidates/{candidate['candidate_id']}/approve",
        json={"content": "默认使用中文回答"},
    ).status_code == 404

    human = next(member for member in room["members"] if member["role"] == "human")
    planner = next(member for member in room["members"] if member["role"] == "planner")
    store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]), recipient_ids=(UUID(human["member_id"]),),
        content="我们决定在 Agent 回复里自动记录", idempotency_key="agent-candidate",
    ))
    store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(human["member_id"]), recipient_ids=(UUID(planner["member_id"]),),
        content="我们决定给飞书开启外部记忆", idempotency_key="external-candidate",
        external_source=ExternalChatSource(
            external_chat_id="chat1", external_sender_id="user1",
        ),
    ))
    assert client.get(path).json()["items"] == [candidate]
    assert source["message"]["message_id"] == candidate["source_message_id"]


def test_manual_save_reconciles_matching_pending_candidate(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    source = _post(client, room_id, "@白金 下次要检查文档链接是否有效")
    _legacy_candidate(store, room_id, source)
    source_id = source["message"]["message_id"]
    candidates_path = f"/api/v1/chats/{room_id}/memory-candidates"
    candidate = client.get(candidates_path).json()["items"][0]
    assert candidate["kind"] == "open_loop"
    memory = client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": source_id,
        "content": "发布前检查文档链接",
    }).json()
    assert client.get(candidates_path).json()["items"] == []
    with store.database.connect() as connection:
        row = connection.execute(
            "SELECT status, memory_id FROM standalone_chat_memory_candidates "
            "WHERE candidate_id = ?", (candidate["candidate_id"],),
        ).fetchone()
    assert (row["status"], row["memory_id"]) == ("approved", memory["memory_id"])


def test_correction_replaces_old_memory_atomically_and_keeps_lineage(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = room["room_id"]
    original = _post(client, room_id, "@白金 我们决定 PR 标题使用 feat 前缀")
    original_id = original["message"]["message_id"]
    old = client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": original_id,
        "content": "PR 标题使用 feat 前缀",
    }).json()
    correction = _post(client, room_id, "@白金 更正：PR 标题改为 fix 前缀")
    _legacy_candidate(store, room_id, correction)
    candidate = client.get(f"/api/v1/chats/{room_id}/memory-candidates").json()["items"][0]
    assert candidate["source_message_id"] == correction["message"]["message_id"]
    path = f"/api/v1/chats/{room_id}/memory-candidates/{candidate['candidate_id']}/approve"
    payload = {"content": "PR 标题使用 fix 前缀", "replaces_memory_id": old["memory_id"]}
    response = client.post(path, json=payload)
    assert response.status_code == 200
    new = response.json()["memory"]
    assert new["kind"] == "correction"
    assert new["replaces_memory_id"] == old["memory_id"]
    assert client.post(path, json=payload).json() == response.json()
    assert client.post(path, json={"content": payload["content"]}).status_code == 409

    active = client.get(f"/api/v1/chats/{room_id}/memories").json()["items"]
    assert active == [new]
    history = client.get(
        f"/api/v1/chats/{room_id}/memories/{new['memory_id']}/history"
    ).json()
    assert [item["memory_id"] for item in history["items"]] == [
        new["memory_id"], old["memory_id"],
    ]
    assert history["items"][1]["status"] == "retired"
    assert history["items"][1]["retired_reason"] == "replaced"
    assert history["truncated"] is False
    assert client.get(
        f"/api/v1/chats/{uuid4()}/memories/{new['memory_id']}/history"
    ).status_code == 404

    restarted = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    restarted.initialize()
    question = _post(client, room_id, "@月见 PR 标题前缀现在是什么？")
    context = json.loads(chat_context(
        restarted, restarted.get_message(UUID(question["message"]["message_id"])),
        restarted.get_room(UUID(room_id)),
    ))
    assert [item["memory_id"] for item in context["room_memories"]] == [new["memory_id"]]
    assert "feat" not in json.dumps(context["room_memories"])
    assert store.memory.retrieve(UUID(room_id), "PR 标题")[0].memory_id == UUID(new["memory_id"])


def test_replacement_requires_active_memory_in_same_room(tmp_path: Path) -> None:
    client, _, room = _setup(tmp_path)
    other = client.post("/api/v1/chats", json={
        "title": "另一个房间", "idempotency_key": str(uuid4()),
    }).json()
    old_source = _post(client, room["room_id"], "@白金 原约定：使用 SQLite")
    old = client.post(f"/api/v1/chats/{room['room_id']}/memories", json={
        "source_message_id": old_source["message"]["message_id"],
        "content": "使用 SQLite",
    }).json()
    other_source = _post(client, other["room_id"], "@白金 更正：使用 PostgreSQL")
    wrong_room = client.post(f"/api/v1/chats/{other['room_id']}/memories", json={
        "source_message_id": other_source["message"]["message_id"],
        "content": "使用 PostgreSQL",
        "replaces_memory_id": old["memory_id"],
    })
    assert wrong_room.status_code == 409
    assert client.get(f"/api/v1/chats/{room['room_id']}/memories").json()["items"] == [old]

    source = _post(client, room["room_id"], "@白金 更正：使用 PostgreSQL")
    manual = client.post(f"/api/v1/chats/{room['room_id']}/memories", json={
        "source_message_id": source["message"]["message_id"],
        "content": "使用 PostgreSQL",
        "replaces_memory_id": old["memory_id"],
    })
    assert manual.status_code == 201
    assert client.post(f"/api/v1/chats/{room['room_id']}/memories", json={
        "source_message_id": source["message"]["message_id"],
        "content": "再使用别的存储",
        "replaces_memory_id": old["memory_id"],
    }).status_code == 409
    assert client.get(f"/api/v1/chats/{room['room_id']}/memories").json()["items"] == [
        manual.json(),
    ]


def test_auto_proposal_enforces_source_scope_and_replaces_old_memory(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    other = client.post("/api/v1/chats", json={
        "title": "其他房间", "idempotency_key": str(uuid4()),
    }).json()
    source = _post(client, str(room_id), "@白金 我们决定 PR 标题用 feat 前缀")
    source_id = UUID(source["message"]["message_id"])
    first = AgentMemoryProposal(kind="decision", content="我们决定 PR 标题用 feat 前缀")
    assert store.memory.capture_agent_proposal(UUID(other["room_id"]), source_id, first) is None
    old = store.memory.capture_agent_proposal(room_id, source_id, first)
    assert old is not None and old.origin == "agent_auto"
    assert store.memory.capture_agent_proposal(room_id, source_id, first) == old

    correction = _post(client, str(room_id), "@白金 更正：PR 标题改为 fix 前缀")
    new = store.memory.capture_agent_proposal(
        room_id, UUID(correction["message"]["message_id"]),
        AgentMemoryProposal(
            kind="correction", content="PR 标题改为 fix 前缀",
            replaces_memory_id=old.memory_id,
        ),
    )
    assert new is not None and new.origin == "agent_auto"
    assert new.replaces_memory_id == old.memory_id
    assert store.memory.list_active(room_id) == (new,)
    lineage = store.memory.lineage(room_id, new.memory_id)
    assert lineage is not None
    assert [item.status for item in lineage[0]] == ["active", "retired"]
    assert store.memory.retire(room_id, new.memory_id).status == "retired"
    assert store.memory.capture_agent_proposal(
        room_id, UUID(correction["message"]["message_id"]),
        AgentMemoryProposal(kind="correction", content="PR 标题改为 fix 前缀"),
    ).status == "retired"
    assert store.memory.list_active(room_id) == ()


def test_auto_proposal_rejects_external_agent_and_sensitive_sources(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    room_id = UUID(room["room_id"])
    human = next(member for member in room["members"] if member["role"] == "human")
    planner = next(member for member in room["members"] if member["role"] == "planner")
    proposal = AgentMemoryProposal(kind="decision", content="统一使用 SQLite")
    secret = _post(client, str(room_id), "@白金 密码是 123，我们决定使用 SQLite")
    assert store.memory.capture_agent_proposal(
        room_id, UUID(secret["message"]["message_id"]), proposal,
    ) is None
    agent = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]), recipient_ids=(UUID(human["member_id"]),),
        content="我们决定使用 SQLite", idempotency_key="agent-auto-memory-source",
    ))
    assert store.memory.capture_agent_proposal(room_id, agent.message.message_id, proposal) is None
    external = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(human["member_id"]), recipient_ids=(UUID(planner["member_id"]),),
        content="我们决定使用 SQLite", idempotency_key="external-auto-memory-source",
        external_source=ExternalChatSource(external_chat_id="chat1", external_sender_id="user1"),
    ))
    assert store.memory.capture_agent_proposal(
        room_id, external.message.message_id, proposal,
    ) is None
    assert store.memory.list_active(room_id) == ()


@pytest.mark.parametrize(
    ("source_text", "kind", "proposed"),
    [
        ("我朋友说他以后只喝冰美式；这不是我的偏好。", "correction",
         "他以后只喝冰美式"),
        ("我朋友以后只喝冰美式。", "preference", "我朋友以后只喝冰美式"),
        ("这不是我的偏好，请不要当成我喜欢冰美式。", "correction",
         "这不是我的偏好"),
        ("我朋友喜欢冰美式，但我喜欢拿铁。", "preference", "我朋友喜欢冰美式"),
        ("在家人共享房间里，家人喜欢冰美式。", "preference",
         "在家人共享房间里，家人喜欢冰美式。"),
        ("在家人共享房间里，他们喜欢冰美式。", "preference",
         "在家人共享房间里，他们喜欢冰美式。"),
        ("在家人共享房间里，这不是我的偏好。", "correction",
         "在家人共享房间里，这不是我的偏好。"),
        ("室友说：在家人共享房间里，以后采购单请按购买地点分组。", "preference",
         "在家人共享房间里，以后采购单请按购买地点分组。"),
        ("在家人喜欢香菜的房间里，以后采购单请按购买地点分组。", "preference",
         "在家人喜欢香菜的房间里，以后采购单请按购买地点分组。"),
    ],
)
def test_auto_proposal_rejects_third_party_or_disavowed_preference(
    tmp_path: Path, source_text: str, kind: str, proposed: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {source_text}")
    room_id = UUID(room["room_id"])
    source_id = UUID(source["message"]["message_id"])
    assert store.memory.capture_agent_proposal(
        room_id, source_id, AgentMemoryProposal(kind=kind, content=proposed),
    ) is None
    assert store.memory.list_active(room_id) == ()
    assert client.get(f"/api/v1/chats/{room_id}/memory-candidates").json()["items"] == []


@pytest.mark.parametrize("content", [
    "在家人共享房间里，以后采购单请按购买地点分组。",
    "在我和家人共享的房间里，以后采购单请按购买地点分组。",
    "在同事共用的聊天房间内，今后发布说明请用三段式。",
])
def test_auto_preference_room_scope_is_not_a_third_party_source(
    tmp_path: Path, content: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {content}")
    room_id = UUID(room["room_id"])
    source_id = UUID(source["message"]["message_id"])
    memory = store.memory.capture_agent_proposal(
        room_id, source_id, AgentMemoryProposal(kind="preference", content=content),
    )
    assert memory is not None
    assert memory.content == content  # Location remains part of the persisted quote.
    assert memory.source_message_id == source_id
    assert store.memory.retrieve(room_id, "采购单 发布说明") == (memory,)
    other = client.post("/api/v1/chats", json={
        "title": "其他房间", "idempotency_key": str(uuid4()),
    }).json()
    assert store.memory.retrieve(UUID(other["room_id"]), "采购单 发布说明") == ()


@pytest.mark.parametrize("source_text", [
    "我朋友喜欢冰美式，但我喜欢拿铁。",
    "在家人共享房间里，家人喜欢冰美式，但我喜欢拿铁。",
])
def test_auto_proposal_keeps_explicit_self_preference_in_mixed_message(
    tmp_path: Path, source_text: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {source_text}")
    room_id = UUID(room["room_id"])
    source_id = UUID(source["message"]["message_id"])
    own = store.memory.capture_agent_proposal(
        room_id, source_id,
        AgentMemoryProposal(kind="preference", content="我喜欢拿铁"),
    )
    assert own is not None
    assert own.content == "我喜欢拿铁"


@pytest.mark.parametrize("content", [
    "镜像安全扫描工具还没有选定。",
    "数据库迁移工具尚未确定。",
    "工具选型待定。",
    "部署方案待拍板。",
    "我们还没决定采用 SQLite，先不比较两种方案。",
    "我们还没决定采用 SQLite，先比较两种方案吗？",
    "镜像扫描工具尚未选定，是否先比较候选工具。",
])
def test_auto_open_loop_rejects_undecided_status_without_action(
    tmp_path: Path, content: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {content}")
    room_id = UUID(room["room_id"])
    assert store.memory.capture_agent_proposal(
        room_id, UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="open_loop", content=content),
    ) is None
    assert store.memory.list_active(room_id) == ()


@pytest.mark.parametrize("content", [
    "我们还没决定采用 SQLite，先比较两种方案。",
    "镜像扫描工具尚未选定，接下来调研候选工具。",
    "镜像扫描工具尚未选定，先检查许可证是否兼容。",
    "部署方案待拍板，之后要核对兼容性。",
])
def test_auto_open_loop_accepts_explicit_action_with_undecided_status(
    tmp_path: Path, content: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {content}")
    room_id = UUID(room["room_id"])
    source_id = UUID(source["message"]["message_id"])
    # Quoting just the unresolved status must not borrow the action elsewhere.
    assert store.memory.capture_agent_proposal(
        room_id, source_id,
        AgentMemoryProposal(kind="open_loop", content=content.split("，")[0]),
    ) is None
    memory = store.memory.capture_agent_proposal(
        room_id, source_id, AgentMemoryProposal(kind="open_loop", content=content),
    )
    assert memory is not None
    assert memory.kind == "open_loop" and memory.open_loop_state == "open"
    assert memory.content == content


def test_auto_proposal_allows_self_correction_without_third_party(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"],
                   "@白金 更正：我以后喝奶茶默认无糖，之前说三分糖不对。")
    memory = store.memory.capture_agent_proposal(
        UUID(room["room_id"]), UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="correction", content="我以后喝奶茶默认无糖"),
    )
    assert memory is not None


@pytest.mark.parametrize(
    ("source_text", "kind", "proposal_content"),
    [
        (
            "下次发布前还需要检查文档里的所有链接是否有效。",
            "open_loop",
            "下次发布前检查文档链接，具体范围待确认",
        ),
        (
            "我们决定测试用例命名统一为 test_ 前缀。",
            "decision",
            "测试用例命名统一为 test_ 前缀，并迁移存量用例、更新 CI",
        ),
        (
            "下次周末出门前还要把相机电池充好。",
            "open_loop",
            "2026-10-09 周五之后的周末出门前把相机电池充好",
        ),
        (
            "以后写 Python 公共函数时请默认补上类型标注。",
            "preference",
            "以后写 Python 公共函数时默认补类型标注，不确定时使用 Any",
        ),
        ("我不喜欢香菜。", "preference", "喜欢香菜"),
        ("我们还没决定采用 SQLite。", "decision", "决定采用 SQLite"),
        ("我喜欢拿铁，但只喝无乳糖的。", "preference", "我喜欢拿铁"),
    ],
)
def test_auto_proposal_rejects_unsupported_expansion(
    tmp_path: Path, source_text: str, kind: str, proposal_content: str,
) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(client, room["room_id"], f"@白金 {source_text}")
    assert store.memory.capture_agent_proposal(
        UUID(room["room_id"]), UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind=kind, content=proposal_content),
    ) is None
    assert store.memory.list_active(UUID(room["room_id"])) == ()


def test_auto_proposal_persists_only_source_quote(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    source = _post(
        client, room["room_id"],
        "@白金 下次发布前还需要检查文档里的所有链接是否有效。",
    )
    memory = store.memory.capture_agent_proposal(
        UUID(room["room_id"]), UUID(source["message"]["message_id"]),
        AgentMemoryProposal(
            kind="open_loop", content="下次发布前还需要检查文档里的所有链接是否有效",
        ),
    )
    assert memory is not None
    assert memory.content == "下次发布前还需要检查文档里的所有链接是否有效"
    assert memory.origin == "agent_auto"


def test_auto_proposal_rejects_oversized_source_quote(tmp_path: Path) -> None:
    client, store, room = _setup(tmp_path)
    quote = "项目决定" + "甲" * 201
    source = _post(client, room["room_id"], f"@白金 {quote}")
    assert store.memory.capture_agent_proposal(
        UUID(room["room_id"]), UUID(source["message"]["message_id"]),
        AgentMemoryProposal(kind="decision", content=quote),
    ) is None
