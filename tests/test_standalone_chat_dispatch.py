"""Fake-agent acceptance for repository-free chat dispatch and safe replay."""

import asyncio
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.models import ChatTurnStatus
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def setup(tmp_path: Path, *, planner=None, implementer=None, reviewer=None, max_turns=6):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    adapters = {
        MemberRole.PLANNER: planner or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"白金建议先确定输入类型","handoff_to":["implementer"]}',
        })),
        MemberRole.IMPLEMENTER: implementer or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"月见建议覆盖空值和 Unicode","handoff_to":["reviewer"]}',
        })),
        MemberRole.REVIEWER: reviewer or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"鲸鲸建议加入回归用例","handoff_to":[]}',
        })),
    }
    runtime = StandaloneChatAgentRuntime(
        StandaloneChatWorkspaceManager(tmp_path / "workspaces", tmp_path / "runtime"),
        adapters,
    )
    dispatcher = StandaloneChatDispatcher(store, runtime, max_turns_per_thread=max_turns)
    return StandaloneChatService(store), dispatcher, adapters


@pytest.mark.asyncio
async def test_http_mention_triggers_bounded_agent_chat_without_task(tmp_path: Path) -> None:
    service, dispatcher, adapters = setup(tmp_path)
    await dispatcher.startup()
    app = create_app(chat_service=service, chat_dispatcher=dispatcher)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        room = (await client.post("/api/v1/chats", json={
            "title": "只读讨论", "idempotency_key": str(uuid4()),
        })).json()
        room_id = room["room_id"]
        payload = {"content": "@白金 请和队友讨论输入校验，不改文件", "idempotency_key": str(uuid4())}
        posted = await client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
        assert posted.status_code == 201
        assert posted.json()["discussion_queued"] is True
        assert posted.json()["execution_authorized"] is False
        assert posted.json()["agent_dispatched"] is False
        await dispatcher.wait_idle()
        turns = (await client.get(f"/api/v1/chats/{room_id}/turns")).json()["items"]
        assert [item["status"] for item in turns] == ["succeeded"] * 3
        messages = (await client.get(f"/api/v1/chats/{room_id}/messages")).json()["items"]
        assert len(messages) == 4
        assert [message["message"]["content"] for message in messages[1:]] == [
            "白金建议先确定输入类型", "月见建议覆盖空值和 Unicode", "鲸鲸建议加入回归用例",
        ]
        assert {message["message"]["correlation_id"] for message in messages} == {
            messages[0]["message"]["correlation_id"]
        }
        await client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
        await dispatcher.wait_idle()
        assert len((await client.get(f"/api/v1/chats/{room_id}/messages")).json()["items"]) == 4
        for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER):
            request = adapters[role].requests[0]
            assert request.discussion_only and request.standalone_chat_room_id == UUID(room_id)
            assert not (request.working_directory / ".git").exists()
            assert not request.working_directory.exists()  # terminal cleanup
    with service.store.database.connect() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tasks'"
        ).fetchone() is None


@pytest.mark.asyncio
async def test_budget_stops_teammate_loop_and_acknowledges_skipped_delivery(tmp_path: Path) -> None:
    service, dispatcher, adapters = setup(tmp_path, max_turns=1)
    await dispatcher.startup()
    room = service.create_room(title="边界", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 @鲸鲸 请讨论", idempotency_key=uuid4(), reply_to=None,
    )
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    turns = service.store.list_turns(room.room_id)
    assert {turn.status for turn in turns} == {
        ChatTurnStatus.SUCCEEDED, ChatTurnStatus.BUDGET_EXHAUSTED,
    }
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert not adapters[MemberRole.REVIEWER].requests


@pytest.mark.asyncio
async def test_failed_and_invalid_outputs_are_not_retried(tmp_path: Path) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(start_error="provider unavailable"))
    service, dispatcher, _ = setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room = service.create_room(title="故障", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    )
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    assert service.store.list_turns(room.room_id)[0].status is ChatTurnStatus.INTERRUPTED
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    assert planner.requests == []
    assert len(service.store.list_messages(room.room_id)) == 1

    invalid = FakeAgentAdapter(FakeAgentScenario(output={
        "message": '{"content":"hello","handoff_to":["planner"]}',
    }))
    other, other_dispatcher, _ = setup(tmp_path / "other", planner=invalid)
    await other_dispatcher.startup()
    other_room = other.create_room(title="无效转交", idempotency_key=uuid4())
    other_dispatcher.enqueue(other.post_message(
        other_room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    ))
    await other_dispatcher.wait_idle()
    assert other.store.list_turns(other_room.room_id)[0].status is ChatTurnStatus.FAILED
    assert len(other.store.list_messages(other_room.room_id)) == 1


@pytest.mark.asyncio
async def test_cancel_running_turn_and_restart_fence_unknown_claim(tmp_path: Path) -> None:
    blocked = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, _ = setup(tmp_path, planner=blocked)
    await dispatcher.startup()
    room = service.create_room(title="取消", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    )
    claim = dispatcher.enqueue(stored)[0]
    for _ in range(100):
        if blocked.requests:
            break
        await asyncio.sleep(0.01)
    assert blocked.requests
    cancelled = await dispatcher.cancel(claim.turn_id)
    assert cancelled.status is ChatTurnStatus.CANCELLED
    assert len(service.store.list_messages(room.room_id)) == 1
    await dispatcher.shutdown()

    another = service.post_message(
        room.room_id, content="@鲸鲸 再看看", idempotency_key=uuid4(), reply_to=None,
    )
    reviewer = next(member for member in room.members if member.role is MemberRole.REVIEWER)
    pending, created = service.store.claim_turn(
        another.message.message_id, reviewer.member_id, max_turns=6,
    )
    assert created and pending.status is ChatTurnStatus.QUEUED
    reopened, restarted, adapters = setup(tmp_path)
    await restarted.startup()
    assert reopened.store.get_turn(pending.turn_id).status is ChatTurnStatus.INTERRUPTED

    running_message = service.post_message(
        room.room_id, content="@月见 请补充", idempotency_key=uuid4(), reply_to=None,
    )
    implementer = next(member for member in room.members if member.role is MemberRole.IMPLEMENTER)
    running, _ = service.store.claim_turn(
        running_message.message.message_id, implementer.member_id, max_turns=6,
    )
    service.store.transition_turn(
        running.turn_id, from_status=ChatTurnStatus.QUEUED,
        to_status=ChatTurnStatus.RUNNING, session_id=uuid4(),
    )
    await restarted.startup()
    assert reopened.store.get_turn(running.turn_id).status is ChatTurnStatus.INTERRUPTED
    restarted.enqueue(another)
    await restarted.wait_idle()
    assert not adapters[MemberRole.REVIEWER].requests
    assert reopened.store.get_turn(pending.turn_id).status is ChatTurnStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_http_cancel_is_scoped_to_its_room(tmp_path: Path) -> None:
    blocked = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, _ = setup(tmp_path, planner=blocked)
    await dispatcher.startup()
    app = create_app(chat_service=service, chat_dispatcher=dispatcher)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rooms = []
        for title in ("one", "two"):
            response = await client.post("/api/v1/chats", json={
                "title": title, "idempotency_key": str(uuid4()),
            })
            rooms.append(response.json()["room_id"])
        post = await client.post(f"/api/v1/chats/{rooms[0]}/messages", json={
            "content": "@白金 请分析", "idempotency_key": str(uuid4()),
        })
        turn_id = post.json()["turns"][0]["turn_id"]
        for _ in range(100):
            if blocked.requests:
                break
            await asyncio.sleep(0.01)
        assert blocked.requests
        assert (await client.post(
            f"/api/v1/chats/{rooms[1]}/turns/{turn_id}/cancel"
        )).status_code == 404
        cancelled = await client.post(f"/api/v1/chats/{rooms[0]}/turns/{turn_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert (await client.get(f"/api/v1/chats/{rooms[0]}/turns")).json()["items"][0][
            "status"
        ] == "cancelled"


def test_fastapi_lifespan_starts_and_stops_chat_dispatcher(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path, max_turns=1)
    with TestClient(create_app(chat_service=service, chat_dispatcher=dispatcher)) as client:
        room_id = client.post("/api/v1/chats", json={
            "title": "生命周期", "idempotency_key": str(uuid4()),
        }).json()["room_id"]
        response = client.post(f"/api/v1/chats/{room_id}/messages", json={
            "content": "@白金 请讨论", "idempotency_key": str(uuid4()),
        })
        assert response.status_code == 201
        message_id = UUID(response.json()["message"]["message"]["message_id"])
        for _ in range(100):
            turn = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"][0]
            if turn["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert turn["status"] == "succeeded"
    assert dispatcher.enqueue(service.store.get_message(message_id)) == ()
