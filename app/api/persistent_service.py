"""Durable task bootstrap and in-process TeamRoom execution for the HTTP API."""

import asyncio
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

from app.api.continuation import preflight_continuation
from app.api.details import (
    ArtifactDetail,
    ContinueTaskPreflight,
    HumanMessageReceipt,
    PlanPage,
    RoomMessagePage,
    TaskControlView,
    TaskRoomView,
)
from app.api.human_messages import build_human_message, message_view
from app.api.models import (
    AuthorizeContinuationRequest,
    CancelContinuationRequest,
    CancelTaskRequest,
    ContinueTaskPreflightRequest,
    ContinueTaskRequest,
    CreateTaskRequest,
    PostHumanMessageRequest,
    QuarantineContinuationRequest,
    TaskPage,
    TaskView,
)
from app.api.service import (
    TaskArtifactIntegrityError,
    TaskArtifactNotFound,
    TaskContinuationNotFound,
    TaskDetailUnavailable,
    TaskInvalidRepository,
    TaskMessageConflict,
    TaskMessageInvalid,
    TaskNotFound,
    TaskServiceUnavailable,
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
    TaskRepositoryIntegrityError,
    TaskSnapshot,
)
from app.storage.continuation_authorizations import ContinuationAuthorizationRepository
from app.storage.continuation_cancellations import ContinuationCancellationRepository
from app.storage.continuation_resumptions import ContinuationResumptionRepository
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationIntent,
    ContinuationNotFoundError,
    ContinuationQuarantineReceipt,
    ContinuationRepository,
    ContinuationState,
    ContinuationStatus,
    human_message_digest,
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
        self.continuations = ContinuationRepository(self.tasks.database)
        self.continuations.initialize()
        self.continuation_cancellations = ContinuationCancellationRepository(self.continuations)
        self.continuation_cancellations.initialize()
        self.continuation_authorizations = ContinuationAuthorizationRepository(self.continuations)
        self.continuation_authorizations.initialize()
        self.continuation_resumptions = ContinuationResumptionRepository(self.continuation_authorizations)
        self.continuation_resumptions.initialize()
        self._continuation_runs = {}
        self._continuation_accepting = True
        self.continuation_cancellation_timeout_seconds = 5.0

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
            result = await self.event_loop.run(runtime, (issue,))
            if getattr(result, "paused", False) and not runtime.task.is_terminal:
                previous = runtime.task.state
                runtime.task.transition_to(TaskState.NEEDS_HUMAN)
                self.router.trace_store.append(TraceEvent(
                    task_id=runtime.task.id, trace_id=runtime.task.trace_id,
                    type=TraceEventType.TASK_STATE_CHANGED,
                    actor_kind=TraceActorKind.DETERMINISTIC, actor_id="task_service",
                    idempotency_key=f"task-human-wait:{runtime.task.id}",
                    payload={"from": previous.value, "to": "needs_human", "reason": "workflow_paused"},
                ))
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
            if (task_id in self._runs or task_id in self._cancelling
                    or any(claim.receipt.request.task_id == task_id and not worker.done()
                           for claim, worker in self._continuation_runs.values())
                    or self.continuations.active_for_task(task_id) is not None):
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

    async def preflight_continue_task(
        self, task_id: UUID, request: ContinueTaskPreflightRequest,
    ) -> ContinueTaskPreflight:
        async with self._lock:
            return preflight_continuation(self, task_id, request)

    async def get_task_control(self, task_id: UUID) -> TaskControlView:
        """Display evidence only; this snapshot grants no execution authority."""
        try:
            task, room = self._task_room(task_id)
            task_revision = self.tasks.get(task_id).revision
            runtime_revision = self.contexts.get(task_id).revision
            guard = self.event_loop.executor.budget_guard
            latest = self.continuations.latest_for_task(task_id)
            outcome = (self.continuations.workflow_outcome(
                task_id=task_id, request_id=latest.receipt.request.request_id,
            ) if latest is not None and latest.receipt.scope == "controlled-workflow-continuation"
                 and latest.receipt.state is ContinuationState.SUCCEEDED else None)
            return TaskControlView(
                task_id=task_id, task_state=task.state,
                task_revision=task_revision, runtime_revision=runtime_revision,
                rework_rounds=task.rework_rounds,
                max_rework_rounds=self.event_loop.controller.max_rework_rounds,
                budget_policy=guard.policy,
                budget_usage=guard.usage(task_id, room_id=room.room_id),
                budget_violation=guard.evaluate(task_id, room_id=room.room_id),
                latest_continuation=latest, latest_workflow_outcome=outcome,
                latest_cancellation=(self.continuation_cancellations.get(
                    task_id=task_id, request_id=latest.receipt.request.request_id,
                ) if latest is not None else None),
            )
        except (ContinuationIntegrityError, RuntimeContextRepositoryError,
                TaskRepositoryIntegrityError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("task control evidence is unavailable") from exc

    async def continue_task(self, task_id: UUID, request: ContinueTaskRequest, *,
                            workflow: bool = False) -> ContinuationStatus:
        from app.agents.registry import AgentRegistryError
        from app.api.continuation_execution import ContinuationExecutionCoordinator
        from app.recovery import EvidenceRecoveryError

        try:
            await self.get_task(task_id)
            return await ContinuationExecutionCoordinator(self).accept(task_id, request, workflow=workflow)

        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("authorization not found for this task") from exc
        except ContinuationConflictError as exc:
            raise TaskStateConflict(str(exc)) from exc
        except (ContinuationIntegrityError, TaskRepositoryIntegrityError, RuntimeContextRepositoryError,
                sqlite3.Error) as exc:
            raise TaskServiceUnavailable("continuation ledger is unavailable") from exc
        except (ArtifactIntegrityError, ArtifactNotFoundError, WorktreeError,
                AgentRegistryError, EvidenceRecoveryError) as exc:
            raise TaskDetailUnavailable("continuation workspace, binding or evidence is unavailable") from exc

    async def get_continuation(self, task_id: UUID, request_id: UUID) -> ContinuationStatus:
        try:
            await self.get_task(task_id)
            return self.continuations.status(task_id=task_id, request_id=request_id)
        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("continuation not found for this task") from exc
        except ContinuationConflictError as exc:
            raise TaskDetailUnavailable(str(exc)) from exc
        except (ContinuationIntegrityError, TaskRepositoryIntegrityError,
                RuntimeContextRepositoryError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("continuation ledger is unavailable") from exc

    async def get_continuation_workflow(self, task_id: UUID, request_id: UUID):
        try:
            await self.get_task(task_id)
            outcome = self.continuations.workflow_outcome(task_id=task_id, request_id=request_id)
            if outcome is None:
                raise TaskContinuationNotFound("workflow result is not committed")
            return outcome
        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("continuation not found for this task") from exc
        except (ContinuationIntegrityError, TaskRepositoryIntegrityError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("workflow result is unavailable") from exc

    async def get_continuation_cancellation(self, task_id: UUID, request_id: UUID):
        try:
            receipt = self.continuation_cancellations.get(task_id=task_id, request_id=request_id)
            if receipt is None:
                raise TaskContinuationNotFound("cancellation not found for this continuation")
            return receipt
        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("continuation not found for this task") from exc
        except (ContinuationIntegrityError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("cancellation ledger is unavailable") from exc

    async def get_continuation_authorization(self, task_id: UUID, authorization_id: UUID):
        try:
            return self.continuation_authorizations.get(task_id=task_id, authorization_id=authorization_id)
        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("authorization not found for this task") from exc
        except (ContinuationIntegrityError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("authorization ledger is unavailable") from exc

    async def authorize_continuation(self, task_id: UUID, request_id: UUID,
                                     request: AuthorizeContinuationRequest):
        from app.api.continuation_runtime import HumanContinuationKernel

        async with self._lock:
            try:
                self.continuations.get_scoped(task_id=task_id, request_id=request_id)
                _, room = self._task_room(task_id)
                humans = [member for member in room.members if member.role is MemberRole.HUMAN]
                if len(humans) != 1 or humans[0].kind is not MemberKind.HUMAN:
                    raise TaskDetailUnavailable("task room must have exactly one Human identity")
                repository = self.continuation_authorizations
                replay = repository.replay(task_id=task_id, previous_request_id=request_id,
                                           command=request, human_member_id=humans[0].member_id)
                if replay is not None:
                    return replay  # Historical receipt, not renewed execution authority.
                prepared = await HumanContinuationKernel(self)._prepare(task_id, ContinueTaskPreflightRequest(
                    expected_revision=request.expected_revision, message_id=request.message_id,
                    target_role=request.target_role.value,
                ))
                checkpoint = prepared.checkpoint
                if checkpoint.runtime_revision != request.expected_runtime_revision:
                    raise TaskStateConflict("authorization runtime revision changed")
                guard = self.event_loop.executor.budget_guard
                if checkpoint.budget_usage.room_messages + 1 >= guard.policy.max_room_messages:
                    raise TaskStateConflict("continuation handoff would exhaust the message budget")
                intent = ContinuationIntent(
                    idempotency_key=request.idempotency_key, task_id=task_id,
                    trace_id=checkpoint.trace_id, room_id=room.room_id, message_id=request.message_id,
                    source_sha256=human_message_digest(prepared.source.message.model_dump(mode="json")),
                    correlation_id=checkpoint.correlation_id, target_role=request.target_role,
                    target_member_id=checkpoint.target_member_id,
                    source_recipient_id=prepared.source.deliveries[0].recipient_id,
                    agent_name=prepared.runtime.agent_names[request.target_role],
                    expected_revision=checkpoint.task_revision, runtime_revision=checkpoint.runtime_revision,
                )
                return repository.authorize(
                    previous_request_id=request_id, human_member_id=humans[0].member_id,
                    command=request, intent=intent, artifacts=prepared.references,
                    runtime_sha256=human_message_digest(self.contexts.get(task_id).context.model_dump(mode="json")),
                )
            except ContinuationNotFoundError as exc:
                raise TaskContinuationNotFound("continuation not found for this task") from exc
            except ContinuationConflictError as exc:
                raise TaskStateConflict(str(exc)) from exc
            except (ContinuationIntegrityError, TaskRepositoryIntegrityError,
                    RuntimeContextRepositoryError, sqlite3.Error) as exc:
                raise TaskServiceUnavailable("authorization ledger is unavailable") from exc

    async def cancel_continuation(self, task_id: UUID, request_id: UUID, request: CancelContinuationRequest):
        # No long workflow lock and no await between durable intent and cancel.
        # Product use requires the same service/event loop that owns the turn.
        try:
            self.continuations.get_scoped(task_id=task_id, request_id=request_id)
            _, room = self._task_room(task_id)
            humans = [member for member in room.members if member.role is MemberRole.HUMAN]
            if len(humans) != 1 or humans[0].kind is not MemberKind.HUMAN:
                raise TaskDetailUnavailable("task room must have exactly one Human identity")
            owned = self._continuation_runs.get(request_id)
            if owned is not None and (owned[0].receipt.request.task_id != task_id
                                      or owned[1].done() or owned[1].get_loop() != asyncio.get_running_loop()):
                owned = None
            receipt, created = self.continuation_cancellations.request(
                task_id=task_id, request_id=request_id, human_member_id=humans[0].member_id,
                command=request, local_claim=owned[0] if owned else None,
            )
            if created:
                owned[1].cancel()
            return receipt
        except ContinuationNotFoundError as exc:
            raise TaskContinuationNotFound("continuation not found for this task") from exc
        except ContinuationConflictError as exc:
            raise TaskStateConflict(str(exc)) from exc
        except (ContinuationIntegrityError, TaskRepositoryIntegrityError,
                RuntimeContextRepositoryError, ValueError, sqlite3.Error) as exc:
            raise TaskServiceUnavailable("cancellation ledger is unavailable") from exc

    async def quarantine_continuation(
        self, task_id: UUID, request_id: UUID, request: QuarantineContinuationRequest,
    ) -> ContinuationQuarantineReceipt:
        async with self._lock:
            try:
                # Resolve scope before room setup, never disclose a request
                # attached to a different task (including incomplete tasks).
                self.continuations.get_scoped(task_id=task_id, request_id=request_id)
                _, room = self._task_room(task_id)
            except ContinuationNotFoundError as exc:
                raise TaskContinuationNotFound("continuation not found for this task") from exc
            except (ContinuationIntegrityError, TaskRepositoryIntegrityError, ValueError,
                    sqlite3.Error) as exc:
                raise TaskServiceUnavailable("quarantine task room is unavailable") from exc
            if task_id in self._runs or task_id in self._cancelling:
                raise TaskStateConflict("local execution/cancellation is active; quarantine is not cancellation")
            humans = [member for member in room.members if member.role is MemberRole.HUMAN]
            if len(humans) != 1 or humans[0].kind is not MemberKind.HUMAN:
                raise TaskDetailUnavailable("task room must have exactly one Human identity")
            try:
                return self.continuations.quarantine(
                    task_id=task_id, request_id=request_id,
                    human_member_id=humans[0].member_id, command=request,
                )
            except ContinuationNotFoundError as exc:
                raise TaskContinuationNotFound("continuation not found for this task") from exc
            except ContinuationConflictError as exc:
                raise TaskStateConflict(str(exc)) from exc
            except (ContinuationIntegrityError, TaskRepositoryIntegrityError,
                    RuntimeContextRepositoryError, sqlite3.Error) as exc:
                raise TaskServiceUnavailable("continuation ledger is unavailable") from exc

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
            if any(claim.receipt.request.task_id == task_id and not worker.done()
                   for claim, worker in self._continuation_runs.values()):
                raise TaskStateConflict("use scoped continuation cancellation for the active owned turn")
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
        self._continuation_accepting = True
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
        self._continuation_accepting = False
        continuations = tuple(run for _, run in self._continuation_runs.values())
        for run in continuations:
            if not run.cancelling():
                run.cancel()
        if continuations:
            await asyncio.gather(*continuations, return_exceptions=True)
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
