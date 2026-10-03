"""P6.4: explicit Human control of a standalone, read-only discussion batch."""

import asyncio
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents import FakeAgentAdapter, FakeAgentScenario, FakeEventSpec
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.bounded_dispatch import BoundedDiscussionDispatcher
from app.chat.discussion_runs import DiscussionRunLimits, DiscussionRunStatus, DiscussionStopReason
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.models import ChatTurnStatus
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.cli import build_chat_app
from app.config import Settings
from app.main import create_app
from app.orchestration.models import utc_now
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def _adapter(content: str, action: str, targets=(), *, delay: float = 0) -> FakeAgentAdapter:
    return FakeAgentAdapter(FakeAgentScenario(
        events=(FakeEventSpec(delay_seconds=delay),) if delay else (),
        output={"structured_output": {
            "content": content, "next_action": action, "handoff_to": list(targets),
        }},
    ))


def _setup(tmp_path: Path, *, planner: FakeAgentAdapter | None = None):
    chat = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    chat.initialize()
    adapters = {
        MemberRole.PLANNER: planner or _adapter("白金交给月见", "handoff", ("implementer",)),
        MemberRole.IMPLEMENTER: _adapter("月见交给鲸鲸", "handoff", ("reviewer",)),
        MemberRole.REVIEWER: _adapter("鲸鲸收尾", "finish"),
    }
    runtime = StandaloneChatAgentRuntime(
        StandaloneChatWorkspaceManager(tmp_path / "workspaces", tmp_path / "runtime"),
        adapters,
    )
    return StandaloneChatService(chat), BoundedDiscussionDispatcher(chat, runtime), adapters


def _root(service: StandaloneChatService):
    room = service.create_room(title="有界接话", idempotency_key=uuid4())
    message = service.post_message(
        room.room_id, content="@白金 请和队友讨论", idempotency_key=uuid4(), reply_to=None,
    )
    return room, message


async def _wait_running(service: StandaloneChatService, room_id: UUID) -> None:
    for _ in range(300):
        turns = service.store.list_turns(room_id)
        if turns and turns[-1].status is ChatTurnStatus.RUNNING:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("Agent turn did not start")


@pytest.mark.asyncio
async def test_pause_finishes_current_turn_then_resumes_fifo_with_same_budget(tmp_path: Path) -> None:
    planner = _adapter("白金交给月见", "handoff", ("implementer",), delay=0.08)
    service, dispatcher, adapters = _setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await _wait_running(service, room.room_id)

    requested = dispatcher.pause(run.run_id)
    assert requested.status is DiscussionRunStatus.RUNNING
    assert requested.pause_requested
    await dispatcher.wait_idle()
    paused = dispatcher.runs.get(run.run_id)
    assert paused.status is DiscussionRunStatus.PAUSED
    assert paused.stop_reason is DiscussionStopReason.HUMAN_PAUSED
    assert paused.agent_turns_used == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert service.store.pending_for(room.members[2].member_id)

    resumed = dispatcher.resume(run.run_id)
    assert resumed.started_at == paused.started_at
    assert resumed.agent_turns_used == 1
    await dispatcher.wait_idle()
    final = dispatcher.runs.get(run.run_id)
    assert final.status is DiscussionRunStatus.FINISHED
    assert final.agent_turns_used == 3
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert len(service.store.list_messages(room.room_id)) == 4


@pytest.mark.asyncio
async def test_cancel_running_turn_requires_confirmed_process_stop(tmp_path: Path) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, adapters = _setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await _wait_running(service, room.room_id)

    cancelled = await dispatcher.cancel(run.run_id)
    assert cancelled.status is DiscussionRunStatus.CANCELLED
    assert cancelled.stop_reason is DiscussionStopReason.HUMAN_CANCELLED
    assert service.store.list_turns(room.room_id)[0].status is ChatTurnStatus.CANCELLED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert (await dispatcher.cancel(run.run_id)) == cancelled


@pytest.mark.asyncio
async def test_uncertain_human_cancel_stops_as_interrupted(tmp_path: Path, monkeypatch) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, adapters = _setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await _wait_running(service, room.room_id)
    original_cancel = dispatcher.runtime.cancel

    async def lost_ack(session_id):
        await original_cancel(session_id)
        raise RuntimeError("cancellation acknowledgement lost")

    monkeypatch.setattr(dispatcher.runtime, "cancel", lost_ack)
    stopped = await dispatcher.cancel(run.run_id)
    assert stopped.status is DiscussionRunStatus.INTERRUPTED
    assert stopped.stop_reason is DiscussionStopReason.UNCERTAIN_RESULT
    assert service.store.list_turns(room.room_id)[0].status is ChatTurnStatus.INTERRUPTED
    assert not adapters[MemberRole.IMPLEMENTER].requests


@pytest.mark.asyncio
async def test_late_agent_output_after_uncertain_cancel_is_not_posted(tmp_path: Path) -> None:
    planner = _adapter("迟到答复", "handoff", ("implementer",), delay=0.08)
    service, dispatcher, adapters = _setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await _wait_running(service, room.room_id)
    _, active = dispatcher.runs.request_cancel(run.run_id)
    assert active is not None
    dispatcher.runs.finish_cancel(run.run_id, active, confirmed=False)

    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.INTERRUPTED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests


@pytest.mark.asyncio
async def test_prestart_pause_survives_restart_and_requires_explicit_resume(tmp_path: Path) -> None:
    service, dispatcher, _ = _setup(tmp_path)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    paused = dispatcher.pause(run.run_id)
    assert paused.status is DiscussionRunStatus.PAUSED
    assert paused.started_at is None

    _, restarted, adapters = _setup(tmp_path)
    await restarted.startup()
    assert restarted.runs.get(run.run_id) == paused
    assert not adapters[MemberRole.PLANNER].requests
    restarted.resume(run.run_id)
    await restarted.wait_idle()
    assert restarted.runs.get(run.run_id).status is DiscussionRunStatus.FINISHED
    assert len(service.store.list_messages(room.room_id)) == 4


@pytest.mark.asyncio
async def test_cancel_before_first_turn_does_not_start_any_agent(tmp_path: Path) -> None:
    service, dispatcher, adapters = _setup(tmp_path)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    cancelled = await dispatcher.cancel(run.run_id)
    assert cancelled.status is DiscussionRunStatus.CANCELLED
    assert cancelled.agent_turns_used == 0
    assert not service.store.list_turns(room.room_id)
    assert all(not adapter.requests for adapter in adapters.values())


@pytest.mark.asyncio
async def test_pause_does_not_extend_elapsed_budget(tmp_path: Path) -> None:
    planner = _adapter("白金交给月见", "handoff", ("implementer",), delay=0.08)
    service, dispatcher, adapters = _setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room, root = _root(service)
    run = dispatcher.start(
        root.message.message_id, opening_role=MemberRole.PLANNER,
        limits=DiscussionRunLimits(max_elapsed_seconds=30),
    )
    await _wait_running(service, room.room_id)
    dispatcher.pause(run.run_id)
    await dispatcher.wait_idle()
    paused = dispatcher.runs.get(run.run_id)
    with dispatcher.runs.database.transaction() as connection:
        row = dispatcher.runs._row(connection, run.run_id)
        now = utc_now()
        aged = paused.model_copy(update={
            "created_at": now - timedelta(seconds=32),
            "started_at": now - timedelta(seconds=31),
        })
        dispatcher.runs._save(connection, aged, dispatcher.runs._pending(row), None)

    stopped = dispatcher.resume(run.run_id)
    assert stopped.status is DiscussionRunStatus.LIMIT_REACHED
    assert stopped.stop_reason is DiscussionStopReason.TIME_LIMIT
    assert stopped.agent_turns_used == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert not service.store.pending_for(room.members[2].member_id)


def test_http_controls_are_explicit_and_room_scoped(tmp_path: Path) -> None:
    service, dispatcher, _ = _setup(
        tmp_path, planner=FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True)),
    )
    app = create_app(chat_service=service, bounded_dispatcher=dispatcher)
    with TestClient(app) as client:
        room = client.post("/api/v1/chats", json={
            "title": "人工控制", "idempotency_key": str(uuid4()),
        }).json()
        room_id = room["room_id"]
        path = f"/api/v1/chats/{room_id}/discussion-runs"
        key = str(uuid4())
        payload = {
            "idempotency_key": key, "opening_role": "planner", "content": "请讨论边界",
        }
        response = client.post(path, json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["execution_authorized"] is False
        run_id = body["run"]["run_id"]
        assert client.post(path, json=payload).json()["run"]["run_id"] == run_id
        assert len(client.get(path).json()["items"]) == 1
        assert client.get(f"{path}/{run_id}").status_code == 200
        assert client.get(f"/api/v1/chats/{uuid4()}/discussion-runs/{run_id}").status_code == 404
        assert client.post(path, json={**payload, "idempotency_key": str(uuid4()),
                                       "opening_role": "human"}).status_code == 422
        for _ in range(300):
            if client.get(f"/api/v1/chats/{room_id}/turns").json()["items"]:
                break
            time.sleep(0.001)
        paused = client.post(f"{path}/{run_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["pause_requested"] is True
        assert client.post(f"{path}/{run_id}/resume").status_code == 200
        cancelled = client.post(f"{path}/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.post(f"{path}/{run_id}/resume").status_code == 409
        assert client.post(f"/api/v1/chats/{uuid4()}/discussion-runs/{run_id}/cancel").status_code == 404


def test_bounded_api_is_unavailable_without_explicit_controller(tmp_path: Path) -> None:
    chat = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    chat.initialize()
    service = StandaloneChatService(chat)
    room = service.create_room(title="旧模式", idempotency_key=uuid4())
    client = TestClient(create_app(chat_service=service))
    assert client.get(f"/api/v1/chats/{room.room_id}/discussion-runs").status_code == 503


@pytest.mark.asyncio
async def test_legacy_startup_fence_does_not_take_bounded_turn(tmp_path: Path) -> None:
    service, bounded, _ = _setup(tmp_path)
    room, root = _root(service)
    run = bounded.runs.create(root, opening_role=MemberRole.PLANNER)
    turn = bounded.runs.claim_next(run.run_id)
    assert turn is not None
    legacy = StandaloneChatDispatcher(service.store, bounded.runtime)
    await legacy.startup()
    assert service.store.get_turn(turn.turn_id).status is ChatTurnStatus.QUEUED
    await bounded.startup()
    assert bounded.runs.get(run.run_id).status is DiscussionRunStatus.INTERRUPTED
    assert service.store.get_turn(turn.turn_id).status is ChatTurnStatus.INTERRUPTED
    assert len(service.store.list_messages(room.room_id)) == 1


def test_fake_cli_app_accepts_explicit_bounded_http_run(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        standalone_chat_workspace_root=tmp_path / "workspaces",
        standalone_chat_runtime_root=tmp_path / "runtime",
    )
    with TestClient(build_chat_app(settings=settings, fake_agents=True)) as client:
        room = client.post("/api/v1/chats", json={
            "title": "三人顺序接话", "idempotency_key": str(uuid4()),
        }).json()
        path = f"/api/v1/chats/{room['room_id']}/discussion-runs"
        response = client.post(path, json={
            "content": "讨论输入边界", "opening_role": "planner",
            "idempotency_key": str(uuid4()),
        })
        assert response.status_code == 201, response.text
        run_id = response.json()["run"]["run_id"]
        for _ in range(300):
            run = client.get(f"{path}/{run_id}").json()
            if run["status"] == "finished":
                break
            time.sleep(0.001)
        assert run["status"] == "finished"
        assert run["agent_turns_used"] == 3
        assert len(client.get(f"/api/v1/chats/{room['room_id']}/messages").json()["items"]) == 4
        assert client.get("/api/v1/tasks").status_code == 503
