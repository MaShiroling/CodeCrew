"""SSE invalidations follow durable chat data and stay room-scoped."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import chats as chat_routes
from app.api.chat_events import stream_chat_activity
from app.chat.models import ChatTurnStatus
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


class ConnectedRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def make_service(tmp_path: Path) -> StandaloneChatService:
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    return StandaloneChatService(store)


def frame_data(frame: str) -> dict[str, int]:
    assert frame.startswith("event: chat_changed\ndata: ")
    return json.loads(frame.split("data: ", 1)[1])


@pytest.mark.asyncio
async def test_chat_sse_follows_messages_and_turn_transitions(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    room = service.create_room(title="SSE", idempotency_key=uuid4())
    request = ConnectedRequest()
    stream = stream_chat_activity(
        request, service, room.room_id, poll_seconds=0.01, heartbeat_seconds=1,
    )
    assert await anext(stream) == "retry: 3000\n\n"
    assert frame_data(await anext(stream)) == {"message_sequence": 0, "turn_count": 0}
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.02)
    assert not waiting.done()

    message = service.post_message(
        room.room_id, content="@白金 请讨论", idempotency_key=uuid4(), reply_to=None,
    )
    assert frame_data(await asyncio.wait_for(waiting, 1)) == {
        "message_sequence": 1, "turn_count": 0,
    }
    planner = next(member for member in room.members if member.role is MemberRole.PLANNER)
    turn, created = service.store.claim_turn(
        message.message.message_id, planner.member_id, max_turns=6,
    )
    assert created
    assert frame_data(await asyncio.wait_for(anext(stream), 1)) == {
        "message_sequence": 1, "turn_count": 1,
    }
    service.store.transition_turn(
        turn.turn_id, from_status=ChatTurnStatus.QUEUED,
        to_status=ChatTurnStatus.RUNNING,
    )
    assert frame_data(await asyncio.wait_for(anext(stream), 1))["turn_count"] == 1
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_chat_sse_rejects_missing_room_before_streaming(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with TestClient(create_app(chat_service=service)) as client:
        missing = client.get(f"/api/v1/chats/{uuid4()}/events")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "chat_not_found"


def test_chat_sse_route_has_stream_headers(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    room = service.create_room(title="events", idempotency_key=uuid4())

    async def finite_stream(_request, _service, _room_id):
        yield "event: chat_changed\ndata: {}\n\n"

    monkeypatch.setattr(chat_routes, "stream_chat_activity", finite_stream)
    with TestClient(create_app(chat_service=service)) as client:
        response = client.get(f"/api/v1/chats/{room.room_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == "event: chat_changed\ndata: {}\n\n"
