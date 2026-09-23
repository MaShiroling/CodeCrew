import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.models import CancelTaskRequest, CreateTaskRequest
from app.api.persistent_service import PersistentTaskService
from app.api.service import TaskInvalidRepository, TaskStateConflict
from app.main import create_app
from app.orchestration.models import Task, TaskState
from app.recovery import EvidenceRecoveryService, WorkflowRecoveryCoordinator
from app.storage import ArtifactStore, RuntimeContextRepository, SQLiteDatabase, TaskRepository
from app.team import ConversationRouter, MemberRole, TeamRoomStore, WorkflowController
from app.verification import VerificationPlan
from app.workspace import WorktreeManager


class ControlledEventLoop:
    def __init__(self, rooms: TeamRoomStore) -> None:
        self.controller = WorkflowController(rooms)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.received = None

    async def run(self, runtime, initial_events):
        self.received = (runtime, initial_events)
        self.started.set()
        await self.release.wait()
        runtime.task.transition_to(TaskState.PLANNING)


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("-c", "user.name=CodeCrew Tests", "-c", "user.email=tests@codecrew.invalid",
         "commit", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


def make_service(tmp_path: Path) -> tuple[PersistentTaskService, ControlledEventLoop]:
    database = SQLiteDatabase(tmp_path / "data.sqlite3")
    tasks = TaskRepository(database)
    contexts = RuntimeContextRepository(database)
    rooms = TeamRoomStore(database)
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    loop = ControlledEventLoop(rooms)
    service = PersistentTaskService(
        tasks=tasks,
        contexts=contexts,
        rooms=rooms,
        router=ConversationRouter(rooms, artifacts),
        worktrees=WorktreeManager(tmp_path / "worktrees"),
        event_loop=loop,
        verification_plan=VerificationPlan(),
        agent_names={
            MemberRole.PLANNER: "planner-fake",
            MemberRole.IMPLEMENTER: "implementer-fake",
            MemberRole.REVIEWER: "reviewer-fake",
        },
    )
    return service, loop


@pytest.mark.asyncio
async def test_create_bootstraps_durable_workflow_and_dispatches(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    service, loop = make_service(tmp_path)

    created = await service.create_task(
        CreateTaskRequest(issue="Fix parser", repository_path=str(repository))
    )
    await asyncio.wait_for(loop.started.wait(), timeout=2)
    runtime, events = loop.received
    assert created.state is TaskState.CREATED
    assert runtime.task.id == created.task_id
    assert service.contexts.get(created.task_id).context.worktree.worktree_path.exists()
    assert events[0].message.content == "Fix parser"
    assert len(events[0].deliveries) == 2
    trace_events = await service.list_trace_events(
        created.task_id, after_sequence=0, limit=100
    )
    assert trace_events
    assert all(item.event.task_id == created.task_id for item in trace_events)
    assert await service.list_trace_events(
        created.task_id, after_sequence=trace_events[-1].sequence, limit=100
    ) == ()
    assert (await service.get_task(created.task_id)).revision == 1
    assert (await service.list_tasks(state=TaskState.CREATED, limit=1, offset=0)).items == (
        created,
    )

    loop.release.set()
    await service.wait_for(created.task_id)
    persisted = await service.get_task(created.task_id)
    assert persisted.state is TaskState.PLANNING
    assert persisted.revision == 2


@pytest.mark.asyncio
async def test_cancel_running_workflow_waits_then_persists_terminal_state(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    service, loop = make_service(tmp_path)
    created = await service.create_task(
        CreateTaskRequest(issue="Fix parser", repository_path=str(repository))
    )
    await asyncio.wait_for(loop.started.wait(), timeout=2)

    cancelled = await service.cancel_task(
        created.task_id, CancelTaskRequest(expected_revision=1, reason="withdrawn")
    )

    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.revision == 2
    assert not loop.release.is_set()
    assert (await service.get_task(created.task_id)).state is TaskState.CANCELLED
    with pytest.raises(TaskStateConflict, match="revision"):
        await service.cancel_task(created.task_id, CancelTaskRequest(expected_revision=1))


@pytest.mark.asyncio
async def test_invalid_repository_leaves_no_task(tmp_path: Path) -> None:
    service, _loop = make_service(tmp_path)
    with pytest.raises(TaskInvalidRepository):
        await service.create_task(
            CreateTaskRequest(issue="Fix parser", repository_path=str(tmp_path / "missing"))
        )
    assert (await service.list_tasks(state=None, limit=20, offset=0)).items == ()


@pytest.mark.asyncio
async def test_cancel_persisted_unstarted_task_and_reject_stale_revision(tmp_path: Path) -> None:
    service, _loop = make_service(tmp_path)
    task = Task(issue="Queued task", repository_path=str(tmp_path))
    service.tasks.create(task)

    cancelled = await service.cancel_task(
        task.id, CancelTaskRequest(expected_revision=1, reason="withdrawn")
    )
    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.revision == 2
    assert service.tasks.get(task.id).task.metadata["cancellation_reason"] == "withdrawn"
    with pytest.raises(TaskStateConflict, match="revision"):
        await service.cancel_task(task.id, CancelTaskRequest(expected_revision=1))


@pytest.mark.asyncio
async def test_startup_dispatches_persisted_unprocessed_issue(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    original, _ = make_service(tmp_path)
    created = await original.create_task(
        CreateTaskRequest(issue="Fix parser", repository_path=str(repository))
    )
    await original.shutdown()

    resumed, loop = make_service(tmp_path)
    recovery = WorkflowRecoveryCoordinator(
        tasks=resumed.tasks,
        contexts=resumed.contexts,
        rooms=resumed.rooms,
        traces=resumed.router.trace_store,
        worktrees=resumed.worktrees,
        evidence=EvidenceRecoveryService(resumed.router.artifacts, resumed.router.trace_store),
        event_loop=loop,
    )
    await resumed.startup(recovery)
    await asyncio.wait_for(loop.started.wait(), timeout=2)
    assert loop.received[0].task.id == created.task_id
    loop.release.set()
    await resumed.wait_for(created.task_id)
    assert (await resumed.get_task(created.task_id)).state is TaskState.PLANNING


def test_http_routes_use_persistent_service(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    service, loop = make_service(tmp_path)
    loop.release.set()
    with TestClient(create_app(task_service=service)) as client:
        created = client.post(
            "/api/v1/tasks", json={"issue": "Fix parser", "repository_path": str(repository)}
        )
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200
        assert client.get("/api/v1/tasks", params={"limit": 1}).json()["items"]
        invalid = client.post(
            "/api/v1/tasks", json={"issue": "Fix parser", "repository_path": str(tmp_path / "bad")}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_repository"
