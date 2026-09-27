"""Durable task bootstrap and in-process TeamRoom execution for the HTTP API."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from app.api.details import (
    ArtifactDetail,
    HumanMessageReceipt,
    PlanPage,
    RoomMessagePage,
    TaskRoomView,
)
from app.api.human_messages import build_human_message, message_view
from app.api.models import (
    CancelTaskRequest,
    CreateTaskRequest,
    PostHumanMessageRequest,
    TaskPage,
    TaskView,
)
from app.api.service import (
    TaskArtifactIntegrityError,
    TaskArtifactNotFound,
    TaskDetailUnavailable,
    TaskInvalidRepository,
    TaskMessageConflict,
    TaskMessageInvalid,
    TaskNotFound,
    TaskStateConflict,
)
from app.orchestration.models import InvalidTaskTransition, Task, TaskState
from app.recovery import RecoveryDisposition, RecoveryEntry, WorkflowRecoveryCoordinator
from app.storage import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    RuntimeContextRepository,
    RuntimeContextRepositoryError,
    StaleTaskRevisionError,
    TaskNotFoundError,
    TaskRepository,
    TaskSnapshot,
)
from app.team.execution import WorkflowEventLoop, WorkflowRuntime
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    RoomStatus,
    StoredChatMessage,
    TeamRoom,
)
from app.team.personas import TeamPersonaCatalog, default_team_personas
from app.team.router import ConversationRouter, ConversationRoutingError
from app.team.store import (
    ChatIdempotencyConflictError,
    RoomConflictError,
    RoomNotFoundError,
    TeamRoomStore,
)
from app.trace import TraceActorKind, TraceEvent, TraceEventType
from app.trace.models import StoredTraceEvent
from app.verification import VerificationPlan
from app.workspace import WorktreeError, WorktreeManager


class PersistentTaskService:
    """Create durable workflow inputs, then dispatch an in-process event loop.

    One service instance owns its active runs; deployment must use a single worker.
    """

    MAX_ARTIFACT_PREVIEW_BYTES = 128 * 1024

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        contexts: RuntimeContextRepository,
        rooms: TeamRoomStore,
        router: ConversationRouter,
        worktrees: WorktreeManager,
        event_loop: WorkflowEventLoop,
        verification_plan: VerificationPlan,
        agent_names: dict[MemberRole, str],
        personas: TeamPersonaCatalog | None = None,
    ) -> None:
        paths = (tasks.database.path, contexts.database.path, rooms.database.path,
                 router.artifacts.database.path)
        if any(path != paths[0] for path in paths):
            raise ValueError("task service stores must share one database")
        if set(agent_names) != {
            MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER
        }:
            raise ValueError("planner, implementer, and reviewer bindings are required")
        self.tasks = tasks
        self.contexts = contexts
        self.rooms = rooms
        self.router = router
        self.worktrees = worktrees
        self.event_loop = event_loop
        self.verification_plan = verification_plan
        self.agent_names = dict(agent_names)
        self.personas = personas or default_team_personas()
        self._runs: dict[UUID, asyncio.Task[None]] = {}
        self._cancelling: set[UUID] = set()
        self._lock = asyncio.Lock()
        self.tasks.initialize()
        self.contexts.initialize()
        self.rooms.initialize()
        self.router.artifacts.initialize()
        self.event_loop.controller.initialize()

    async def create_task(self, request: CreateTaskRequest) -> TaskView:
        task = Task(issue=request.issue, repository_path=request.repository_path)
        try:
            worktree = await self.worktrees.create(
                task_id=task.id, repository=Path(request.repository_path)
            )
        except WorktreeError as exc:
            raise TaskInvalidRepository(str(exc)) from exc

        room_id = uuid4()
        members = tuple(
            RoomMember(room_id=room_id, name=name, role=role, kind=kind)
            for name, role, kind in (
                (self.personas.for_role(MemberRole.PLANNER).display_name,
                 MemberRole.PLANNER, MemberKind.AGENT),
                (self.personas.for_role(MemberRole.IMPLEMENTER).display_name,
                 MemberRole.IMPLEMENTER, MemberKind.AGENT),
                (self.personas.for_role(MemberRole.REVIEWER).display_name,
                 MemberRole.REVIEWER, MemberKind.AGENT),
                ("verifier", MemberRole.VERIFIER, MemberKind.SYSTEM),
                ("orchestrator", MemberRole.ORCHESTRATOR, MemberKind.SYSTEM),
                ("human", MemberRole.HUMAN, MemberKind.HUMAN),
            )
        )
        room = TeamRoom(
            room_id=room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            name=f"task-{task.id}",
            members=members,
        )
        runtime = WorkflowRuntime(
            task=task,
            room_id=room_id,
            worktree=worktree,
            verification_plan=self.verification_plan,
            agent_names=self.agent_names,
        )
        snapshot = self.tasks.create(task)
        try:
            self.rooms.create_room(room)
            self.contexts.create(runtime.to_context())
            human_id = next(member.member_id for member in members
                            if member.role is MemberRole.HUMAN)
            issue = self.router.route(
                ChatMessage(
                    room_id=room_id,
                    task_id=task.id,
                    trace_id=task.trace_id,
                    sender_id=human_id,
                    recipients=(
                        MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),
                        MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.ORCHESTRATOR),
                    ),
                    type=MessageType.ISSUE_POSTED,
                    content=task.issue,
                    correlation_id=task.trace_id,
                    idempotency_key=f"initial-issue:{task.id}",
                ),
                authenticated_sender_id=human_id,
            )
        except Exception as exc:
            self._mark_needs_human(snapshot, f"task bootstrap failed: {type(exc).__name__}: {exc}")
            raise

        async with self._lock:
            self._runs[task.id] = asyncio.create_task(
                self._run(runtime, issue), name=f"codecrew-task-{task.id}"
            )
        return self._view(snapshot)

    async def _run(self, runtime: WorkflowRuntime, issue: StoredChatMessage) -> None:
        try:
            await self.event_loop.run(runtime, (issue,))
            snapshot = self.tasks.get(runtime.task.id)
            if runtime.task != snapshot.task:
                self.tasks.save(runtime.task, expected_revision=snapshot.revision)
            context = self.contexts.get(runtime.task.id)
            self.contexts.save(runtime.to_context(), expected_revision=context.revision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate background workflow failures
            snapshot = self.tasks.get(runtime.task.id)
            self._mark_needs_human(
                snapshot, f"workflow execution failed: {type(exc).__name__}: {exc}"
            )
        finally:
            async with self._lock:
                self._runs.pop(runtime.task.id, None)

    async def get_task(self, task_id: UUID) -> TaskView:
        try:
            return self._view(self.tasks.get(task_id))
        except TaskNotFoundError as exc:
            raise TaskNotFound(str(exc)) from exc

    async def list_tasks(
        self, *, state: TaskState | None, limit: int, offset: int
    ) -> TaskPage:
        snapshots = self.tasks.list(state=state, limit=limit + 1, offset=offset)
        return TaskPage(
            items=tuple(self._view(item) for item in snapshots[:limit]),
            limit=limit,
            offset=offset,
            next_offset=offset + limit if len(snapshots) > limit else None,
        )

    async def list_trace_events(
        self, task_id: UUID, *, after_sequence: int, limit: int
    ) -> tuple[StoredTraceEvent, ...]:
        try:
            snapshot = self.tasks.get(task_id)
        except TaskNotFoundError as exc:
            raise TaskNotFound(str(exc)) from exc
        return self.router.trace_store.list(
            task_id=task_id,
            trace_id=snapshot.task.trace_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def _task_room(self, task_id: UUID) -> tuple[Task, TeamRoom]:
        try:
            task = self.tasks.get(task_id).task
        except TaskNotFoundError as exc:
            raise TaskNotFound(str(exc)) from exc
        try:
            context = self.contexts.get(task_id).context
            room = self.rooms.get_room(context.room_id)
        except (RuntimeContextRepositoryError, RoomNotFoundError) as exc:
            raise TaskDetailUnavailable("task room is not available") from exc
        if (
            context.trace_id != task.trace_id
            or room.task_id != task.id
            or room.trace_id != task.trace_id
        ):
            raise TaskDetailUnavailable("task room identity does not match the task")
        return task, room

    async def get_room(self, task_id: UUID) -> TaskRoomView:
        _, room = self._task_room(task_id)
        return TaskRoomView(room=room)

    async def post_human_message(
        self, task_id: UUID, request: PostHumanMessageRequest,
    ) -> HumanMessageReceipt:
        # Single worker, no await inside validation/write; no state or budget changes.
        async with self._lock:
            task, room = self._task_room(task_id)
            snapshot = self.tasks.get(task_id)
            if snapshot.revision != request.expected_revision:
                raise TaskStateConflict("task revision changed")
            if task_id in self._runs or task_id in self._cancelling:
                raise TaskStateConflict("task execution or cancellation is still active")
            if task.state is not TaskState.NEEDS_HUMAN or room.status is not RoomStatus.ACTIVE:
                raise TaskStateConflict("human messages require a paused needs_human task and active room")
            message = build_human_message(task, room, self.rooms, request)
            try:
                stored = self.router.route(message, authenticated_sender_id=message.sender_id)
            except ChatIdempotencyConflictError as exc:
                raise TaskMessageConflict("idempotency key was used for different human intent") from exc
            except RoomConflictError as exc:
                raise TaskMessageConflict("task room changed") from exc
            except ConversationRoutingError as exc:
                raise TaskMessageInvalid("human message is not allowed in this room") from exc
            return HumanMessageReceipt(message=message_view(stored, room), task_revision=snapshot.revision)

    async def list_room_messages(
        self, task_id: UUID, *, after_sequence: int, limit: int
    ) -> RoomMessagePage:
        _, room = self._task_room(task_id)
        stored = self.rooms.list_messages(
            room.room_id, after_sequence=after_sequence, limit=limit + 1
        )
        items = [message_view(item, room) for item in stored[:limit]]
        return RoomMessagePage(
            items=tuple(items),
            limit=limit,
            after_sequence=after_sequence,
            next_after_sequence=items[-1].sequence if len(stored) > limit else None,
        )

    async def list_plans(self, task_id: UUID) -> PlanPage:
        _, room = self._task_room(task_id)
        return PlanPage(items=self.rooms.list_plan_revisions(room.room_id))

    async def get_artifact(self, task_id: UUID, artifact_id: UUID) -> ArtifactDetail:
        try:
            task = self.tasks.get(task_id).task
        except TaskNotFoundError as exc:
            raise TaskNotFound(str(exc)) from exc
        try:
            metadata = self.router.artifacts.get_metadata(artifact_id)
        except ArtifactNotFoundError as exc:
            raise TaskArtifactNotFound("artifact not found for this task") from exc
        if metadata.task_id != task.id or metadata.trace_id != task.trace_id:
            raise TaskArtifactNotFound("artifact not found for this task")
        if metadata.size_bytes > self.MAX_ARTIFACT_PREVIEW_BYTES:
            return ArtifactDetail(metadata=metadata, preview_unavailable_reason="too_large")
        try:
            content = self.router.artifacts.read_bytes(artifact_id)
        except ArtifactIntegrityError as exc:
            raise TaskArtifactIntegrityError("artifact content failed integrity check") from exc
        if not (
            metadata.media_type.startswith("text/")
            or metadata.media_type == "application/json"
            or metadata.media_type.endswith("+json")
        ):
            return ArtifactDetail(metadata=metadata, preview_unavailable_reason="binary")
        try:
            preview = content.decode("utf-8")
        except UnicodeDecodeError:
            return ArtifactDetail(metadata=metadata, preview_unavailable_reason="not_utf8")
        return ArtifactDetail(metadata=metadata, preview=preview)

    async def cancel_task(
        self, task_id: UUID, request: CancelTaskRequest
    ) -> TaskView:
        async with self._lock:
            try:
                snapshot = self.tasks.get(task_id)
            except TaskNotFoundError as exc:
                raise TaskNotFound(str(exc)) from exc
            if snapshot.revision != request.expected_revision:
                raise TaskStateConflict("task revision changed")
            if task_id in self._cancelling:
                raise TaskStateConflict("task cancellation is already in progress")
            if snapshot.task.is_terminal:
                raise TaskStateConflict("terminal task cannot be cancelled")
            self._cancelling.add(task_id)
            run = self._runs.get(task_id)
            if run is not None:
                run.cancel()
        try:
            if run is not None:
                try:
                    await run
                except asyncio.CancelledError:
                    pass
            snapshot = self.tasks.get(task_id)
            if snapshot.revision != request.expected_revision:
                raise TaskStateConflict("task revision changed while cancelling")
            task = snapshot.task
            try:
                task.transition_to(TaskState.CANCELLED)
            except InvalidTaskTransition as exc:
                raise TaskStateConflict(str(exc)) from exc
            if request.reason:
                task.metadata["cancellation_reason"] = request.reason
            try:
                saved = self.tasks.save(task, expected_revision=snapshot.revision)
            except StaleTaskRevisionError as exc:
                raise TaskStateConflict(str(exc)) from exc
            self.router.trace_store.append(
                TraceEvent(
                    task_id=task.id,
                    trace_id=task.trace_id,
                    type=TraceEventType.TASK_STATE_CHANGED,
                    actor_kind=TraceActorKind.HUMAN,
                    actor_id="task_api",
                    idempotency_key=f"task-cancelled:{task.id}",
                    payload={"from": snapshot.task.state.value, "to": TaskState.CANCELLED.value},
                )
            )
            return self._view(saved)
        finally:
            async with self._lock:
                self._cancelling.discard(task_id)
                if run is not None and run.done():
                    self._runs.pop(task_id, None)

    async def startup(self, recovery: WorkflowRecoveryCoordinator) -> None:
        """Classify persisted tasks and dispatch only unambiguous pending events."""
        entries = await recovery.scan()
        async with self._lock:
            for entry in entries:
                if entry.disposition is not RecoveryDisposition.RESUMABLE:
                    continue
                if entry.task_id in self._runs or entry.task_id in self._cancelling:
                    continue
                self._runs[entry.task_id] = asyncio.create_task(
                    self._resume(recovery, entry), name=f"codecrew-recovery-{entry.task_id}"
                )

    async def _resume(
        self, recovery: WorkflowRecoveryCoordinator, entry: RecoveryEntry
    ) -> None:
        try:
            await recovery.resume(entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate recovered task failures
            snapshot = self.tasks.get(entry.task_id)
            self._mark_needs_human(
                snapshot, f"workflow resume failed: {type(exc).__name__}: {exc}"
            )
        finally:
            async with self._lock:
                self._runs.pop(entry.task_id, None)

    async def shutdown(self) -> None:
        """Stop local executions without declaring persisted tasks cancelled."""
        async with self._lock:
            runs = tuple(self._runs.values())
            for run in runs:
                run.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)

    async def wait_for(self, task_id: UUID) -> None:
        """Wait for a locally dispatched run; useful for controlled shutdown/tests."""
        async with self._lock:
            run = self._runs.get(task_id)
        if run is not None:
            await run

    def _mark_needs_human(self, snapshot: TaskSnapshot, reason: str) -> None:
        task = snapshot.task
        if task.is_terminal:
            return
        task.transition_to(TaskState.NEEDS_HUMAN)
        task.metadata["failure_reason"] = reason
        self.tasks.save(task, expected_revision=snapshot.revision)
        self.router.trace_store.append(
            TraceEvent(
                task_id=task.id,
                trace_id=task.trace_id,
                type=TraceEventType.SYSTEM_ERROR,
                actor_kind=TraceActorKind.SYSTEM,
                actor_id="task_service",
                idempotency_key=f"task-service-error:{task.id}",
                payload={"reason": reason},
            )
        )

    @staticmethod
    def _view(snapshot: TaskSnapshot) -> TaskView:
        task = snapshot.task
        return TaskView(
            task_id=task.id,
            trace_id=task.trace_id,
            issue=task.issue,
            repository_path=task.repository_path,
            state=task.state,
            rework_rounds=task.rework_rounds,
            revision=snapshot.revision,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
