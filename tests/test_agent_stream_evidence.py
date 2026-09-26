import asyncio
from pathlib import Path

import pytest

from app.agents import (
    AgentEventType,
    AgentRegistry,
    AgentRole,
    CodexCliAdapter,
    FakeAgentScenario,
    FakeEventSpec,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.storage import ArtifactStore, SQLiteDatabase
from app.team import AgentTurnError, AgentTurnRunner, MemberRole
from app.trace import TraceEventType
from scripts.smoke_evidence import archive_smoke_evidence
from tests.test_agent_turn_runner import make_context, send_trigger
from tests.test_codex_adapter import StubProcess, StubRunner, json_chunk


def recorded_stream(router, artifacts, task):
    records = router.trace_store.list(
        trace_id=task.trace_id, type=TraceEventType.AGENT_STREAM_RECORDED
    )
    assert len(records) == 1
    event = records[0].event
    return event, artifacts.read_json(event.payload["artifact_id"])


@pytest.mark.asyncio
async def test_codex_timeout_preserves_received_stderr_errors_and_native_session(tmp_path):
    _, router, rooms, artifacts, _, task, room, members = make_context(tmp_path, FakeAgentScenario())
    stderr = "model discovery waiting\nrequest transport waiting\n"
    process = StubProcess(
        [
            json_chunk({"type": "thread.started", "thread_id": "offline-thread"}),
            ProcessChunk(ProcessStream.STDERR, stderr),
            json_chunk({"type": "error", "message": "Reconnecting... request timed out"}),
        ],
        ProcessResult(exit_code=-15, duration_ms=180016, timed_out=True),
    )
    process_runner = StubRunner(process)
    adapter = CodexCliAdapter(runner=process_runner)
    registry = AgentRegistry()
    registry.register(adapter, roles={AgentRole.PLANNER}, permission_modes={PermissionMode.READ_ONLY})
    runner = AgentTurnRunner(registry, router, timeout_seconds=180)
    planner = members[MemberRole.PLANNER]
    trigger = send_trigger(router, room, members[MemberRole.ORCHESTRATOR], planner)
    with pytest.raises(AgentTurnError, match="timed_out"):
        await runner.run(
            task, room_id=room.room_id, member_id=planner.member_id,
            agent_name=adapter.name, working_directory=tmp_path,
        )
    event, stream = recorded_stream(router, artifacts, task)
    assert stream["outcome"] == "timed_out" and stream["stream_complete"] is True
    assert stream["native_session_id"] == "offline-thread"
    assert [e["text"] for e in stream["events"] if e["type"] == "stderr"] == [stderr]
    assert any(e["native_event_type"] == "error" for e in stream["events"])
    assert [e["sequence"] for e in stream["events"]] == list(range(len(stream["events"])))
    assert event.correlation_id == trigger.message.correlation_id
    assert event.causation_id == trigger.message.message_id
    assert event.payload["stderr_event_count"] == 1
    assert "request transport" not in event.model_dump_json()  # Large text stays in the Artifact.
    assert rooms.pending_for(planner.member_id) == (trigger,)
    assert len(process_runner.calls) == 1
    assert process_runner.calls[0]["timeout_seconds"] == 180
    raw = router.trace_store.list(trace_id=task.trace_id, type=TraceEventType.AGENT_OUTPUT_RECORDED)
    assert artifacts.read_json(raw[0].event.payload["artifact_id"])["reason"] == "timed_out"
    archive = archive_smoke_evidence(artifacts, task, root=tmp_path / "archives")
    archived = ArtifactStore(SQLiteDatabase(archive / "trace.sqlite3"), archive / "artifacts")
    assert archived.read_json(event.payload["artifact_id"]) == stream
    assert not router.trace_store.list(trace_id=task.trace_id, type=TraceEventType.COMPLETION_DECIDED)


@pytest.mark.asyncio
async def test_success_records_exact_normalized_events_without_changing_return(tmp_path):
    runner, router, _, artifacts, adapter, task, room, members = make_context(
        tmp_path,
        FakeAgentScenario(
            events=(FakeEventSpec(type=AgentEventType.STDERR, text="diagnostic only\n"),),
            output={"actions": [{"action": "finish_turn"}]},
        ),
    )
    member = members[MemberRole.IMPLEMENTER]
    send_trigger(router, room, members[MemberRole.ORCHESTRATOR], member)
    result = await runner.run(
        task, room_id=room.room_id, member_id=member.member_id,
        agent_name=adapter.name, working_directory=tmp_path,
    )
    event, stream = recorded_stream(router, artifacts, task)
    assert stream["outcome"] == "completed" and stream["stream_complete"]
    assert stream["events"] == [e.model_dump(mode="json") for e in result.events]
    assert event.payload["event_count"] == len(result.events)


@pytest.mark.asyncio
async def test_stream_exception_saves_prefix_cancels_adapter_and_preserves_error(tmp_path):
    runner, router, rooms, artifacts, adapter, task, room, members = make_context(
        tmp_path, FakeAgentScenario(block_until_cancel=True)
    )
    original = adapter.stream
    cancelled = []
    cancel = adapter.cancel

    async def broken_stream(session_id):
        async for event in original(session_id):
            yield event
            raise RuntimeError("synthetic streaming failure")

    async def tracked_cancel(session_id):
        cancelled.append(session_id)
        await cancel(session_id)

    adapter.stream = broken_stream
    adapter.cancel = tracked_cancel
    member = members[MemberRole.IMPLEMENTER]
    trigger = send_trigger(router, room, members[MemberRole.ORCHESTRATOR], member)
    with pytest.raises(RuntimeError, match="synthetic streaming failure"):
        await runner.run(
            task, room_id=room.room_id, member_id=member.member_id,
            agent_name=adapter.name, working_directory=tmp_path,
        )
    _, stream = recorded_stream(router, artifacts, task)
    assert stream["outcome"] == "adapter_exception" and not stream["stream_complete"]
    assert len(stream["events"]) == len(cancelled) == 1
    assert rooms.pending_for(member.member_id) == (trigger,)
    assert not router.trace_store.list(trace_id=task.trace_id, type=TraceEventType.AGENT_OUTPUT_RECORDED)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_caller_cancellation_records_only_received_prefix(tmp_path, cleanup_fails):
    runner, router, rooms, artifacts, adapter, task, room, members = make_context(
        tmp_path,
        FakeAgentScenario(
            events=(FakeEventSpec(type=AgentEventType.STDERR, text="waiting\n"),),
            block_until_cancel=True,
        ),
    )
    ready = asyncio.Event()
    original = adapter.stream

    async def tracked_stream(session_id):
        async for event in original(session_id):
            yield event
            if event.type is AgentEventType.STDERR:
                ready.set()

    adapter.stream = tracked_stream
    if cleanup_fails:
        original_cancel = adapter.cancel

        async def bad_cleanup(session_id):
            await original_cancel(session_id)
            raise OSError("sensitive cleanup detail")

        adapter.cancel = bad_cleanup
    member = members[MemberRole.IMPLEMENTER]
    trigger = send_trigger(router, room, members[MemberRole.ORCHESTRATOR], member)
    turn = asyncio.create_task(runner.run(
        task, room_id=room.room_id, member_id=member.member_id,
        agent_name=adapter.name, working_directory=tmp_path,
    ))
    await asyncio.wait_for(ready.wait(), timeout=2)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError) as failure:
        await turn
    if cleanup_fails:
        assert failure.value.__notes__ == ["Agent cancellation failed: OSError"]
    _, stream = recorded_stream(router, artifacts, task)
    assert stream["outcome"] == "cancelled" and not stream["stream_complete"]
    assert [e["type"] for e in stream["events"]] == ["started", "stderr"]
    assert rooms.pending_for(member.member_id) == (trigger,)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_fails", [False, True])
async def test_recording_failure_never_acks_or_hides_stream_exception(tmp_path: Path, stream_fails):
    runner, router, rooms, artifacts, adapter, task, room, members = make_context(
        tmp_path, FakeAgentScenario(output={"actions": [{"action": "finish_turn"}]})
    )
    if stream_fails:
        original = adapter.stream

        async def broken_stream(session_id):
            async for event in original(session_id):
                yield event
                raise RuntimeError("original stream failure")

        adapter.stream = broken_stream

    def broken_record(*args, **kwargs):
        raise OSError("sensitive sink detail")

    artifacts.put_json = broken_record
    member = members[MemberRole.IMPLEMENTER]
    trigger = send_trigger(router, room, members[MemberRole.ORCHESTRATOR], member)
    with pytest.raises(RuntimeError if stream_fails else OSError) as failure:
        await runner.run(
            task, room_id=room.room_id, member_id=member.member_id,
            agent_name=adapter.name, working_directory=tmp_path,
        )
    if stream_fails:
        assert str(failure.value) == "original stream failure"
        assert failure.value.__notes__ == ["Agent stream diagnostic recording failed: OSError"]
    assert rooms.pending_for(member.member_id) == (trigger,)
