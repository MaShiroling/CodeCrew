import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents import AgentRole
from app.orchestration.models import Task, TaskState
from app.recovery import (
    EvidenceRecoveryService,
    RecoveryDisposition,
    WorkflowRecoveryCoordinator,
)
from app.storage import (
    AgentRuntimeBinding,
    ArtifactStore,
    RuntimeContextRepository,
    SQLiteDatabase,
    TaskRepository,
    WorkflowRuntimeContext,
)
from app.team import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    TeamRoom,
    TeamRoomStore,
)
from app.trace import TraceActorKind, TraceEvent, TraceEventType, TraceStore
from app.verification import VerificationPlan
from app.workspace import WorktreeManager


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("test\n")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "initial",
    )
    return path.resolve()


class _FakeEventLoop:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, runtime, initial_events):
        self.calls.append(initial_events)
        runtime.task.transition_to(TaskState.PLANNING)
        return SimpleNamespace(task=runtime.task)


async def _setup(tmp_path: Path, *, create_context: bool = True):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    tasks = TaskRepository(database)
    contexts = RuntimeContextRepository(database)
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    rooms = TeamRoomStore(database)
    traces = TraceStore(database)
    for store in (tasks, contexts, artifacts, rooms, traces):
        store.initialize()

    repository = _repository(tmp_path / "repository")
    task = Task(issue="resume after restart", repository_path=str(repository))
    task_snapshot = tasks.create(task)
    room_id = uuid4()
    members = (
        RoomMember(
            room_id=room_id,
            name="orchestrator",
            role=MemberRole.ORCHESTRATOR,
            kind=MemberKind.SYSTEM,
        ),
        RoomMember(
            room_id=room_id,
            name="human",
            role=MemberRole.HUMAN,
            kind=MemberKind.HUMAN,
        ),
        RoomMember(
            room_id=room_id,
            name="planner",
            role=MemberRole.PLANNER,
            kind=MemberKind.AGENT,
        ),
    )
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Coding task",
        members=members,
    )
    rooms.create_room(room)
    worktrees = WorktreeManager(tmp_path / "worktrees")
    handle = await worktrees.create(task_id=task.id, repository=repository)
    if create_context:
        contexts.create(
            WorkflowRuntimeContext(
                task_id=task.id,
                trace_id=task.trace_id,
                room_id=room_id,
                worktree=handle,
                verification_plan=VerificationPlan(),
                agent_bindings=(
                    AgentRuntimeBinding(
                        role=AgentRole.PLANNER,
                        agent_name="fake-planner",
                    ),
                ),
            )
        )
    loop = _FakeEventLoop()
    coordinator = WorkflowRecoveryCoordinator(
        tasks=tasks,
        contexts=contexts,
        rooms=rooms,
        traces=traces,
        worktrees=worktrees,
        evidence=EvidenceRecoveryService(artifacts, traces),
        event_loop=loop,
    )
    return coordinator, task_snapshot.task, members, rooms, traces, tasks, contexts, loop


def _post_issue(task, room, members, rooms):
    orchestrator, human, _ = members
    message = ChatMessage(
        room_id=room.room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        sender_id=human.member_id,
        recipients=(
            MessageRecipient(
                kind=RecipientKind.MEMBER,
                member_id=orchestrator.member_id,
            ),
        ),
        type=MessageType.ISSUE_POSTED,
        content=task.issue,
        idempotency_key="issue-recovery-test",
    )
    return rooms.append_message(message, recipient_ids=(orchestrator.member_id,))


@pytest.mark.asyncio
async def test_scan_and_resume_pending_event_persists_task_and_runtime(
    tmp_path: Path,
) -> None:
    coordinator, task, members, rooms, traces, tasks, contexts, loop = await _setup(tmp_path)
    room = rooms.get_room(contexts.get(task.id).context.room_id)
    pending = _post_issue(task, room, members, rooms)

    entries = await coordinator.scan()
    repeated = await coordinator.scan()
    assert len(entries) == 1
    assert entries[0].disposition is RecoveryDisposition.RESUMABLE
    assert entries[0].pending_events == (pending,)
    assert repeated[0].disposition is RecoveryDisposition.RESUMABLE
    assert (
        len(
            traces.list(
                task_id=task.id,
                trace_id=task.trace_id,
                type=TraceEventType.RECOVERY_DECIDED,
                limit=10,
            )
        )
        == 1
    )

    resumed = await coordinator.resume(entries[0])

    assert len(loop.calls) == 1
    assert loop.calls[0] == (pending,)
    assert resumed.task_revision == 2
    assert resumed.runtime_revision == 2
    assert tasks.get(task.id).task.state is TaskState.PLANNING


@pytest.mark.asyncio
async def test_scan_escalates_missing_context_to_human(tmp_path: Path) -> None:
    coordinator, _, *_ = await _setup(tmp_path, create_context=False)

    entries = await coordinator.scan()

    assert entries[0].disposition is RecoveryDisposition.NEEDS_HUMAN
    assert "runtime context is missing" in entries[0].reason


@pytest.mark.asyncio
async def test_scan_does_not_replay_event_with_existing_workflow_decision(
    tmp_path: Path,
) -> None:
    coordinator, task, members, rooms, traces, tasks, _, loop = await _setup(tmp_path)
    room = rooms.get_room(coordinator.contexts.get(task.id).context.room_id)
    pending = _post_issue(task, room, members, rooms)
    traces.append(
        TraceEvent(
            task_id=task.id,
            trace_id=task.trace_id,
            type=TraceEventType.WORKFLOW_DECISION,
            actor_kind=TraceActorKind.DETERMINISTIC,
            actor_id="workflow_controller",
            idempotency_key="already-decided",
            payload={"message_id": str(pending.message.message_id)},
        )
    )

    entries = await coordinator.scan()

    assert entries[0].disposition is RecoveryDisposition.NEEDS_HUMAN
    assert "execution outcome is ambiguous" in entries[0].reason
    assert tasks.get(task.id).task.state is TaskState.NEEDS_HUMAN
    assert loop.calls == []


@pytest.mark.asyncio
async def test_startup_resume_failure_escalates_task_and_records_trace(
    tmp_path: Path,
) -> None:
    coordinator, task, members, rooms, traces, tasks, contexts, _ = await _setup(tmp_path)
    room = rooms.get_room(contexts.get(task.id).context.room_id)
    _post_issue(task, room, members, rooms)

    class _FailingEventLoop:
        async def run(self, runtime, initial_events):
            raise RuntimeError("agent process died during recovery")

    coordinator.event_loop = _FailingEventLoop()
    report = await coordinator.recover_startup()

    assert len(report.failures) == 1
    assert tasks.get(task.id).task.state is TaskState.NEEDS_HUMAN
    recorded = traces.list(task_id=task.id, trace_id=task.trace_id, limit=50)
    assert any(item.event.type is TraceEventType.RECOVERY_DECIDED for item in recorded)
    assert any(item.event.type is TraceEventType.TASK_STATE_CHANGED for item in recorded)


def _run_recovery_in_new_process(tmp_path: Path) -> dict:
    program = r"""
import asyncio
import json
import sys
from pathlib import Path

from app.orchestration.models import TaskState
from app.recovery import EvidenceRecoveryService, WorkflowRecoveryCoordinator
from app.storage import ArtifactStore, RuntimeContextRepository, SQLiteDatabase, TaskRepository
from app.team import MemberRole, TeamRoomStore
from app.trace import TraceStore
from app.workspace import WorktreeManager

database = SQLiteDatabase(Path(sys.argv[1]))
tasks = TaskRepository(database)
contexts = RuntimeContextRepository(database)
artifacts = ArtifactStore(database, Path(sys.argv[2]) / "artifacts")
rooms = TeamRoomStore(database)
traces = TraceStore(database)
for store in (tasks, contexts, artifacts, rooms, traces):
    store.initialize()

class RestartedEventLoop:
    async def run(self, runtime, events):
        runtime.task.transition_to(TaskState.PLANNING)
        orchestrator = next(
            member for member in rooms.get_room(runtime.room_id).members
            if member.role is MemberRole.ORCHESTRATOR
        )
        for event in events:
            rooms.acknowledge(event.message.message_id, recipient_id=orchestrator.member_id)
        runtime.native_session_ids[MemberRole.PLANNER] = "planner-session-after-restart"
        return {"processed": len(events)}

coordinator = WorkflowRecoveryCoordinator(
    tasks=tasks,
    contexts=contexts,
    rooms=rooms,
    traces=traces,
    worktrees=WorktreeManager(Path(sys.argv[2]) / "worktrees"),
    evidence=EvidenceRecoveryService(artifacts, traces),
    event_loop=RestartedEventLoop(),
)

async def main():
    report = await coordinator.recover_startup()
    task = tasks.list(limit=1)[0].task
    context = contexts.get(task.id) if task.state is not TaskState.NEEDS_HUMAN else None
    print(json.dumps({
        "dispositions": [entry.disposition.value for entry in report.entries],
        "resumed_count": len(report.resumed),
        "failures": len(report.failures),
        "task_state": task.state.value,
        "runtime_revision": context.revision if context else None,
        "planner_session": next((
            binding.native_session_id for binding in context.context.agent_bindings
            if binding.role.value == "planner"
        ), None) if context else None,
    }))

asyncio.run(main())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(tmp_path / "codecrew.sqlite3"),
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.asyncio
async def test_recovery_resumes_task_after_real_process_restart(tmp_path: Path) -> None:
    _, task, members, rooms, _, tasks, contexts, _ = await _setup(tmp_path)
    room = rooms.get_room(contexts.get(task.id).context.room_id)
    _post_issue(task, room, members, rooms)

    result = _run_recovery_in_new_process(tmp_path)

    assert result == {
        "dispositions": ["resumable"],
        "resumed_count": 1,
        "failures": 0,
        "task_state": "planning",
        "runtime_revision": 2,
        "planner_session": "planner-session-after-restart",
    }
    assert tasks.get(task.id).task.state is TaskState.PLANNING
    assert contexts.get(task.id).revision == 2


@pytest.mark.asyncio
async def test_recovery_escalates_missing_context_after_real_process_restart(
    tmp_path: Path,
) -> None:
    _, _, *_ = await _setup(tmp_path, create_context=False)

    result = _run_recovery_in_new_process(tmp_path)

    assert result["dispositions"] == ["needs_human"]
    assert result["resumed_count"] == 0
    assert result["task_state"] == "needs_human"
