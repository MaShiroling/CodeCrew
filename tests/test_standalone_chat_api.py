"""HTTP chat contracts must not create coding tasks or dispatch Agents."""

from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.chat.models import StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def make_client(tmp_path: Path) -> tuple[TestClient, StandaloneChatStore]:
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    return TestClient(create_app(chat_service=StandaloneChatService(store))), store


def test_create_list_and_get_room_are_task_independent(tmp_path: Path) -> None:
    client, store = make_client(tmp_path)
    request = {"title": "讨论输入校验", "idempotency_key": str(uuid4())}
    response = client.post("/api/v1/chats", json=request)
    assert response.status_code == 201
    room = response.json()
    assert {member["role"] for member in room["members"]} == {
        "human", "planner", "implementer", "reviewer",
    }
    assert "task_id" not in room and "repository_path" not in room
    assert client.post("/api/v1/chats", json=request).json() == room
    assert client.get("/api/v1/chats").json()["items"] == [room]
    assert client.get(f"/api/v1/chats/{room['room_id']}").json() == room
    assert client.post("/api/v1/chats", json={**request, "title": "other"}).status_code == 409
    assert client.get(f"/api/v1/chats/{uuid4()}").status_code == 404
    with store.database.connect() as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert "tasks" not in tables
    assert "team_rooms" not in tables
    assert not (tmp_path / "worktrees").exists()


def test_human_mentions_message_replay_and_validation(tmp_path: Path) -> None:
    client, store = make_client(tmp_path)
    room = client.post("/api/v1/chats", json={
        "title": "讨论", "idempotency_key": str(uuid4()),
    }).json()
    room_id = room["room_id"]
    payload = {"content": "@白金 @月见 请讨论输入校验", "idempotency_key": str(uuid4())}
    first = client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
    assert first.status_code == 201
    receipt = first.json()
    assert receipt["agent_dispatched"] is False
    assert receipt["execution_authorized"] is False
    message = receipt["message"]
    assert len(message["message"]["recipient_ids"]) == 2
    assert message["sequence"] == 1
    assert client.post(f"/api/v1/chats/{room_id}/messages", json=payload).json() == receipt
    page = client.get(f"/api/v1/chats/{room_id}/messages").json()
    assert page["items"] == [message]
    assert client.get(f"/api/v1/chats/{room_id}/messages?after_sequence=1").json()["items"] == []
    assert client.post(f"/api/v1/chats/{room_id}/messages", json={
        **payload, "content": "@白金 another intent",
    }).status_code == 409
    for content in ("没有提及", "@nobody please help", "@白金"):
        assert client.post(f"/api/v1/chats/{room_id}/messages", json={
            "idempotency_key": str(uuid4()), "content": content,
        }).status_code == 422
    assert client.get(f"/api/v1/chats/{uuid4()}/messages").status_code == 404
    assert client.post(f"/api/v1/chats/{uuid4()}/messages", json=payload).status_code == 404
    assert len(store.list_messages(UUID(room_id))) == 1


def test_reply_addresses_agent_author_and_keeps_correlation(tmp_path: Path) -> None:
    client, store = make_client(tmp_path)
    room = client.post("/api/v1/chats", json={
        "title": "讨论", "idempotency_key": str(uuid4()),
    }).json()
    room_id = UUID(room["room_id"])
    initial = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": "@白金 请分析边界", "idempotency_key": str(uuid4()),
    }).json()["message"]["message"]
    planner = next(member for member in room["members"] if member["role"] == "planner")
    human = next(member for member in room["members"] if member["role"] == "human")
    agent_message = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]), recipient_ids=(UUID(human["member_id"]),),
        content="建议空值保持原行为", reply_to=UUID(initial["message_id"]),
        causation_id=UUID(initial["message_id"]),
        correlation_id=UUID(initial["correlation_id"]), idempotency_key="test-agent-reply",
    )).message
    payload = {"content": "明白了，也请 @鲸鲸 看看", "idempotency_key": str(uuid4()),
               "reply_to": str(agent_message.message_id)}
    reply = client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
    assert reply.status_code == 201
    posted = reply.json()["message"]["message"]
    assert posted["reply_to"] == str(agent_message.message_id)
    assert posted["correlation_id"] == str(agent_message.correlation_id)
    assert posted["causation_id"] == str(agent_message.message_id)
    role_by_id = {member["member_id"]: member["role"] for member in room["members"]}
    assert tuple(role_by_id[item] for item in posted["recipient_ids"]) == (
        MemberRole.PLANNER.value, MemberRole.REVIEWER.value,
    )
    assert client.post(f"/api/v1/chats/{room_id}/messages", json=payload).json() == reply.json()
    assert client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": "reply", "idempotency_key": str(uuid4()),
        "reply_to": initial["message_id"],
    }).status_code == 422
    other_room = client.post("/api/v1/chats", json={
        "title": "另一个房间", "idempotency_key": str(uuid4()),
    }).json()
    assert client.post(f"/api/v1/chats/{other_room['room_id']}/messages", json={
        "content": "跨房间回复", "idempotency_key": str(uuid4()),
        "reply_to": str(agent_message.message_id),
    }).status_code == 404


def test_explicit_context_anchor_is_human_same_room_and_idempotent(tmp_path: Path) -> None:
    client, store = make_client(tmp_path)
    room = client.post("/api/v1/chats", json={
        "title": "当前房间", "idempotency_key": str(uuid4()),
    }).json()
    other = client.post("/api/v1/chats", json={
        "title": "另一个房间", "idempotency_key": str(uuid4()),
    }).json()
    path = f"/api/v1/chats/{room['room_id']}/messages"
    original = client.post(path, json={
        "content": "@白金 最初目标", "idempotency_key": str(uuid4()),
    }).json()["message"]["message"]
    key = str(uuid4())
    payload = {"content": "@月见 继续目标", "idempotency_key": key,
               "context_anchor_id": original["message_id"]}
    response = client.post(path, json=payload)
    assert response.status_code == 201
    anchored = response.json()["message"]["message"]
    assert anchored["context_anchor_id"] == original["message_id"]
    assert anchored["correlation_id"] != original["correlation_id"]
    assert client.post(path, json=payload).json() == response.json()
    assert client.post(path, json={**payload, "context_anchor_id": None}).status_code == 409
    assert client.post(f"/api/v1/chats/{other['room_id']}/messages", json={
        **payload, "idempotency_key": str(uuid4()),
    }).status_code == 404
    assert client.post(path, json={
        **payload, "idempotency_key": str(uuid4()),
        "context_anchor_id": str(uuid4()),
    }).status_code == 404
    planner = next(member for member in room["members"] if member["role"] == "planner")
    human = next(member for member in room["members"] if member["role"] == "human")
    agent = store.append_message(StandaloneChatMessage(
        room_id=UUID(room["room_id"]), trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]), recipient_ids=(UUID(human["member_id"]),),
        content="Agent 答复", reply_to=UUID(original["message_id"]),
        context_anchor_id=UUID(original["message_id"]),
        correlation_id=UUID(original["correlation_id"]), idempotency_key="agent-anchor-test",
    )).message
    reply = client.post(path, json={
        "content": "继续解释", "reply_to": str(agent.message_id),
        "idempotency_key": str(uuid4()),
    })
    assert reply.status_code == 201
    assert reply.json()["message"]["message"]["context_anchor_id"] == original["message_id"]
    assert client.post(path, json={
        "content": "@月见 不能同时引用", "reply_to": str(agent.message_id),
        "context_anchor_id": original["message_id"],
        "idempotency_key": str(uuid4()),
    }).status_code == 422
    assert client.post(path, json={
        **payload, "idempotency_key": str(uuid4()),
        "context_anchor_id": str(agent.message_id),
    }).status_code == 422


def test_chat_api_is_unavailable_without_a_chat_service() -> None:
    client = TestClient(create_app())
    assert client.get("/api/v1/chats").status_code == 503
