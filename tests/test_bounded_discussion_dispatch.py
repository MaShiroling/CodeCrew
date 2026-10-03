"""P6.2: durable, sequential, opt-in Agent handoffs in standalone chat."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.agents import AgentExitReason, FakeAgentAdapter, FakeAgentScenario
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.bounded_dispatch import BoundedDiscussionDispatcher
from app.chat.discussion_runs import (
    DiscussionRunLimits,
    DiscussionRunStatus,
    DiscussionStopReason,
)
from app.chat.discussion_store import DiscussionRunStore
from app.chat.models import ChatTurnStatus, StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatConflictError, StandaloneChatStore
from app.orchestration.models import utc_now
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def fake(content: str, action: str, targets: tuple[str, ...] = ()) -> FakeAgentAdapter:
    return FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "structured_output": {
                    "content": content,
                    "next_action": action,
                    "handoff_to": list(targets),
                },
            }
        )
    )


def setup(tmp_path: Path, adapters=None):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    adapters = adapters or {
        MemberRole.PLANNER: fake("白金：先拆边界", "handoff", ("implementer",)),
        MemberRole.IMPLEMENTER: fake("月见：补实现条件", "handoff", ("reviewer",)),
        MemberRole.REVIEWER: fake("鲸鲸：检查风险", "finish"),
    }
    runtime = StandaloneChatAgentRuntime(
        StandaloneChatWorkspaceManager(tmp_path / "workspaces", tmp_path / "runtime"),
        adapters,
    )
    return StandaloneChatService(store), BoundedDiscussionDispatcher(store, runtime), adapters


def root_message(service: StandaloneChatService, *, title: str = "方案讨论"):
    room = service.create_room(title=title, idempotency_key=uuid4())
    root = service.post_message(
        room.room_id,
        content="@白金 请三人轮流讨论，不改代码",
        idempotency_key=uuid4(),
        reply_to=None,
    )
    return room, root


def test_concurrent_claims_reserve_only_one_active_turn(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path)
    _, root = root_message(service)
    run = dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    other_chat = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    other_chat.initialize()
    other_runs = DiscussionRunStore(other_chat)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda runs: runs.claim_next(run.run_id),
                (dispatcher.runs, other_runs),
            )
        )
    assert sum(claim is not None for claim in claims) == 1
    assert dispatcher.runs.get(run.run_id).agent_turns_used == 1
    assert len(service.store.list_turns(run.room_id)) == 1


@pytest.mark.asyncio
async def test_structured_handoffs_are_serial_and_persisted(tmp_path: Path) -> None:
    service, dispatcher, adapters = setup(tmp_path)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    assert (
        dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER).run_id
        == run.run_id
    )
    await dispatcher.wait_idle()

    saved = dispatcher.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.FINISHED
    assert saved.stop_reason is DiscussionStopReason.AGENT_FINISHED
    assert saved.agent_turns_used == 3
    messages = service.store.list_messages(room.room_id)
    assert [message.message.content for message in messages] == [
        "@白金 请三人轮流讨论，不改代码",
        "白金：先拆边界",
        "月见：补实现条件",
        "鲸鲸：检查风险",
    ]
    assert len({message.message.correlation_id for message in messages}) == 1
    assert [turn.status for turn in service.store.list_turns(room.room_id)] == [
        ChatTurnStatus.SUCCEEDED,
    ] * 3
    assert all(len(adapter.requests) == 1 for adapter in adapters.values())
    assert all(
        request.discussion_only for adapter in adapters.values() for request in adapter.requests
    )
    assert all(
        not request.working_directory.exists()
        for adapter in adapters.values()
        for request in adapter.requests
    )
    with service.store.database.connect() as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchone()
            is None
        )

    reopened, fresh, _ = setup(tmp_path)
    await fresh.startup()
    assert fresh.runs.get(run.run_id) == saved
    assert len(reopened.store.list_messages(room.room_id)) == 4
    assert fresh.start(root.message.message_id, opening_role=MemberRole.PLANNER) == saved
    await fresh.wait_idle()
    assert len(reopened.store.list_messages(room.room_id)) == 4


@pytest.mark.asyncio
async def test_same_agent_can_speak_twice_and_turn_limit_stops_cycle(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: fake("白金继续", "handoff", ("implementer",)),
        MemberRole.IMPLEMENTER: fake("月见接话", "handoff", ("planner",)),
        MemberRole.REVIEWER: fake("鲸鲸待命", "finish"),
    }
    service, dispatcher, adapters = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(
        root.message.message_id,
        opening_role=MemberRole.PLANNER,
        limits=DiscussionRunLimits(max_agent_turns=3, max_elapsed_seconds=60),
    )
    await dispatcher.wait_idle()

    saved = dispatcher.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.LIMIT_REACHED
    assert saved.stop_reason is DiscussionStopReason.TURN_LIMIT
    assert saved.agent_turns_used == 3
    assert [item.message.content for item in service.store.list_messages(room.room_id)[1:]] == [
        "白金继续",
        "月见接话",
        "白金继续",
    ]
    assert len(adapters[MemberRole.PLANNER].requests) == 2
    assert len(adapters[MemberRole.IMPLEMENTER].requests) == 1
    assert len(adapters[MemberRole.REVIEWER].requests) == 0
    assert len(service.store.list_turns(room.room_id)) == 3
    assert not service.store.pending_for(room.members[1].member_id)
    assert not service.store.pending_for(room.members[2].member_id)


@pytest.mark.asyncio
async def test_two_target_handoff_uses_fifo_not_parallel_fanout(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: fake("白金分派", "handoff", ("implementer", "reviewer")),
        MemberRole.IMPLEMENTER: fake("月见回应", "handoff", ("planner",)),
        MemberRole.REVIEWER: fake("鲸鲸回应", "handoff", ("implementer",)),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(
        root.message.message_id,
        opening_role=MemberRole.PLANNER,
        limits=DiscussionRunLimits(max_agent_turns=4, max_elapsed_seconds=60),
    )
    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.LIMIT_REACHED
    assert [m.message.content for m in service.store.list_messages(room.room_id)[1:]] == [
        "白金分派",
        "月见回应",
        "鲸鲸回应",
        "白金分派",
    ]
    assert len(service.store.list_turns(room.room_id)) == 4
    assert all(
        not service.store.pending_for(member.member_id)
        for member in room.members
        if member.role is not MemberRole.HUMAN
    )


@pytest.mark.asyncio
async def test_prose_mention_does_not_schedule_a_turn(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: fake("@月见 请先等 Human 补充", "await_human"),
        MemberRole.IMPLEMENTER: fake("不应回复", "finish"),
        MemberRole.REVIEWER: fake("不应回复", "finish"),
    }
    service, dispatcher, adapters = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.AWAITING_HUMAN
    assert len(service.store.list_messages(room.room_id)) == 2
    assert not adapters[MemberRole.IMPLEMENTER].requests


@pytest.mark.asyncio
async def test_restart_fences_claim_and_does_not_relaunch(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    claimed = dispatcher.runs.claim_next(run.run_id)
    assert claimed is not None and claimed.status is ChatTurnStatus.QUEUED

    reopened, restarted, adapters = setup(tmp_path)
    await restarted.startup()
    saved = restarted.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.INTERRUPTED
    assert saved.stop_reason is DiscussionStopReason.SERVER_RESTART
    assert reopened.store.get_turn(claimed.turn_id).status is ChatTurnStatus.INTERRUPTED
    assert restarted.runs.claim_next(run.run_id) is None
    assert restarted.start(root.message.message_id, opening_role=MemberRole.PLANNER) == saved
    await restarted.wait_idle()
    assert all(not adapter.requests for adapter in adapters.values())
    assert len(reopened.store.list_messages(room.room_id)) == 1


@pytest.mark.asyncio
async def test_restart_fences_reply_saved_before_handoff_schedule(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    turn = dispatcher.runs.claim_next(run.run_id)
    assert turn is not None
    assert service.store.transition_turn(
        turn.turn_id,
        from_status=ChatTurnStatus.QUEUED,
        to_status=ChatTurnStatus.RUNNING,
    )
    service.store.append_message(
        StandaloneChatMessage(
            room_id=room.room_id,
            trace_id=room.trace_id,
            sender_id=room.members[1].member_id,
            recipient_ids=(room.members[0].member_id, room.members[2].member_id),
            content="已保存但尚未排队",
            reply_to=root.message.message_id,
            causation_id=root.message.message_id,
            correlation_id=root.message.correlation_id,
            idempotency_key=f"bounded-run:{run.run_id}:turn:{turn.turn_id}",
        )
    )
    assert service.store.pending_for(room.members[2].member_id)
    _, restarted, adapters = setup(tmp_path)
    await restarted.startup()
    assert restarted.runs.get(run.run_id).status is DiscussionRunStatus.INTERRUPTED
    assert not service.store.pending_for(room.members[2].member_id)
    assert all(not adapter.requests for adapter in adapters.values())


@pytest.mark.asyncio
async def test_invalid_decision_fails_without_scheduling_or_writing(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(
            FakeAgentScenario(
                output={
                    "message": '{"content":"@月见 快接话","next_action":"finish",'
                    '"handoff_to":["implementer"],"write_paths":["src"]}',
                }
            )
        ),
        MemberRole.IMPLEMENTER: fake("不应被叫醒", "finish"),
        MemberRole.REVIEWER: fake("不应被叫醒", "finish"),
    }
    service, dispatcher, adapters = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.FAILED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests


@pytest.mark.asyncio
async def test_one_shot_and_bounded_modes_cannot_share_one_root(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path)
    await dispatcher.startup()
    _, root = root_message(service)
    with pytest.raises(StandaloneChatConflictError, match="fresh Human message"):
        dispatcher.runs.create(root, opening_role=MemberRole.REVIEWER)

    from app.chat.dispatch import StandaloneChatDispatcher

    old = StandaloneChatDispatcher(service.store, dispatcher.runtime)
    claims = old.enqueue(root)
    assert len(claims) == 1
    with pytest.raises(StandaloneChatConflictError, match="one-shot"):
        dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
    await old.wait_idle()

    fresh_room, fresh_root = root_message(service, title="另一话题")
    bounded = dispatcher.runs.create(fresh_root, opening_role=MemberRole.PLANNER)
    assert old.enqueue(fresh_root) == ()
    assert dispatcher.runs.get(bounded.run_id).status is DiscussionRunStatus.CREATED
    assert not service.store.list_turns(fresh_room.room_id)


@pytest.mark.asyncio
async def test_shutdown_interrupts_running_batch(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True)),
        MemberRole.IMPLEMENTER: fake("未运行", "finish"),
        MemberRole.REVIEWER: fake("未运行", "finish"),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    for _ in range(100):
        if service.store.list_turns(room.room_id):
            break
        await asyncio.sleep(0.001)
    await dispatcher.shutdown()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.INTERRUPTED
    assert service.store.list_turns(room.room_id)[0].status is ChatTurnStatus.INTERRUPTED


def _age_running_batch(dispatcher: BoundedDiscussionDispatcher, run_id, seconds: float) -> None:
    """Bring a minimum-30-second batch close to its deadline without sleeping."""
    with dispatcher.runs.database.transaction() as connection:
        row = dispatcher.runs._row(connection, run_id)
        run = dispatcher.runs._run(row)
        now = utc_now()
        aged = run.model_copy(update={
            "created_at": now - timedelta(seconds=seconds + 1),
            "started_at": now - timedelta(seconds=seconds),
        })
        dispatcher.runs._save(
            connection, aged, dispatcher.runs._pending(row),
            UUID(row["active_turn_id"]),
        )


@pytest.mark.asyncio
async def test_running_turn_hits_wall_deadline_and_cancels_agent(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True)),
        MemberRole.IMPLEMENTER: fake("不应运行", "finish"),
        MemberRole.REVIEWER: fake("不应运行", "finish"),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.runs.create(
        root, opening_role=MemberRole.PLANNER,
        limits=DiscussionRunLimits(max_elapsed_seconds=30),
    )
    turn = dispatcher.runs.claim_next(run.run_id)
    assert turn is not None
    _age_running_batch(dispatcher, run.run_id, 29.8)

    assert not await dispatcher._execute(run.run_id, turn)
    saved = dispatcher.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.LIMIT_REACHED
    assert saved.stop_reason is DiscussionStopReason.TIME_LIMIT
    assert service.store.get_turn(turn.turn_id).status is ChatTurnStatus.BUDGET_EXHAUSTED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests


@pytest.mark.asyncio
async def test_unconfirmed_deadline_cancel_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True)),
        MemberRole.IMPLEMENTER: fake("不应运行", "finish"),
        MemberRole.REVIEWER: fake("不应运行", "finish"),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.runs.create(
        root, opening_role=MemberRole.PLANNER,
        limits=DiscussionRunLimits(max_elapsed_seconds=30),
    )
    turn = dispatcher.runs.claim_next(run.run_id)
    assert turn is not None
    _age_running_batch(dispatcher, run.run_id, 29.8)
    original_cancel = dispatcher.runtime.cancel

    async def unconfirmed_cancel(session_id):
        await original_cancel(session_id)
        raise RuntimeError("cancel acknowledgement was lost")

    monkeypatch.setattr(dispatcher.runtime, "cancel", unconfirmed_cancel)
    assert not await dispatcher._execute(run.run_id, turn)
    saved = dispatcher.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.INTERRUPTED
    assert saved.stop_reason is DiscussionStopReason.UNCERTAIN_RESULT
    assert service.store.get_turn(turn.turn_id).status is ChatTurnStatus.INTERRUPTED
    assert len(service.store.list_messages(room.room_id)) == 1


@pytest.mark.asyncio
async def test_per_turn_timeout_fails_without_claiming_time_limit(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True)),
        MemberRole.IMPLEMENTER: fake("不应运行", "finish"),
        MemberRole.REVIEWER: fake("不应运行", "finish"),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    dispatcher.timeout_seconds = 1
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await dispatcher.wait_idle()
    saved = dispatcher.runs.get(run.run_id)
    assert saved.status is DiscussionRunStatus.FAILED
    assert saved.stop_reason is DiscussionStopReason.AGENT_FAILED
    assert service.store.list_turns(room.room_id)[0].status is ChatTurnStatus.FAILED
    assert len(service.store.list_messages(room.room_id)) == 1


@pytest.mark.asyncio
async def test_scheduler_error_fences_unclaimed_run(tmp_path: Path, monkeypatch) -> None:
    service, dispatcher, adapters = setup(tmp_path)
    await dispatcher.startup()
    room, root = root_message(service)

    def broken_claim(_run_id):
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(dispatcher.runs, "claim_next", broken_claim)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.INTERRUPTED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert not service.store.list_turns(room.room_id)
    assert all(not adapter.requests for adapter in adapters.values())


@pytest.mark.asyncio
async def test_agent_failure_does_not_route_queued_handoffs(tmp_path: Path) -> None:
    adapters = {
        MemberRole.PLANNER: FakeAgentAdapter(
            FakeAgentScenario(reason=AgentExitReason.FAILED, exit_code=1),
        ),
        MemberRole.IMPLEMENTER: fake("不应运行", "finish"),
        MemberRole.REVIEWER: fake("不应运行", "finish"),
    }
    service, dispatcher, _ = setup(tmp_path, adapters)
    await dispatcher.startup()
    room, root = root_message(service)
    run = dispatcher.start(root.message.message_id, opening_role=MemberRole.PLANNER)
    await dispatcher.wait_idle()
    assert dispatcher.runs.get(run.run_id).status is DiscussionRunStatus.FAILED
    assert len(service.store.list_messages(room.room_id)) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
