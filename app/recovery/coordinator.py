import hashlib
import sqlite3
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.orchestration.models import TERMINAL_STATES, TaskState
from app.recovery.evidence import EvidenceRecoveryError, EvidenceRecoveryService
from app.storage.continuations import (
    ContinuationIntegrityError,
    ContinuationRepository,
    ContinuationState,
)
from app.storage.runtime import (
    RuntimeContextIntegrityError,
    RuntimeContextNotFoundError,
    RuntimeContextRepository,
)
from app.storage.tasks import (
    TaskRepository,
    TaskSnapshot,
)
from app.team.execution import WorkflowEventLoop, WorkflowRuntime
from app.team.models import MemberRole, StoredChatMessage
from app.team.store import MemberNotFoundError, RoomNotFoundError, TeamRoomStore
from app.trace.models import TraceActorKind, TraceEvent, TraceEventType
from app.trace.store import TraceStore
from app.workspace.worktrees import WorktreeError, WorktreeManager


class RecoveryDisposition(str, Enum):
    RESUMABLE = "resumable"
    WAITING = "waiting"
    NEEDS_HUMAN = "needs_human"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    task_id: UUID
    disposition: RecoveryDisposition
    reason: str
    task_revision: int
    runtime_revision: int | None = None
    runtime: WorkflowRuntime | None = None
    pending_events: tuple[StoredChatMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryRun:
    entry: RecoveryEntry
    workflow_result: object
    task_revision: int
    runtime_revision: int


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    entries: tuple[RecoveryEntry, ...]
    resumed: tuple[RecoveryRun, ...]
    failures: tuple[tuple[UUID, str], ...] = ()


class WorkflowRecoveryCoordinator:
    """Scans persisted tasks and safely resumes unambiguous pending workflow events."""

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        contexts: RuntimeContextRepository,
        rooms: TeamRoomStore,
        traces: TraceStore,
        worktrees: WorktreeManager,
        evidence: EvidenceRecoveryService,
        event_loop: WorkflowEventLoop,
    ) -> None:
        databases = (
            tasks.database.path,
            contexts.database.path,
            rooms.database.path,
            traces.database.path,
            evidence.artifacts.database.path,
        )
        if any(path != databases[0] for path in databases):
            raise ValueError("recovery coordinator stores must share one database")
        self.tasks = tasks
        self.contexts = contexts
        self.rooms = rooms
        self.traces = traces
        self.worktrees = worktrees
        self.evidence = evidence
        self.event_loop = event_loop

    async def scan(self, *, page_size: int = 100) -> tuple[RecoveryEntry, ...]:
        """Classify every persisted task without running Agents or commands."""
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        entries: list[RecoveryEntry] = []
        offset = 0
        while True:
            page = self.tasks.list(limit=page_size, offset=offset)
            for snapshot in page:
                entries.append(await self._inspect_task(snapshot))
            if len(page) < page_size:
                return tuple(entries)
            offset += len(page)

    async def resume(self, entry: RecoveryEntry) -> RecoveryRun:
        """Run only events previously classified as unambiguous and persist outputs."""
        if entry.disposition is not RecoveryDisposition.RESUMABLE:
            raise RecoveryCoordinatorError(
                f"task {entry.task_id} is {entry.disposition.value}, not resumable"
            )
        if entry.runtime is None or not entry.pending_events:
            raise RecoveryCoordinatorError("resumable entry is missing runtime events")

        snapshot = self.tasks.get(entry.task_id)
        if snapshot.revision != entry.task_revision:
            raise RecoveryCoordinatorError(f"task {entry.task_id} changed after recovery scan")
        context_snapshot = self.contexts.get(entry.task_id)
        if context_snapshot.revision != entry.runtime_revision:
            raise RecoveryCoordinatorError(
                f"runtime context for task {entry.task_id} changed after recovery scan"
            )
        self._require_no_continuation_reservation(entry.task_id)
        room = self.rooms.get_room(entry.runtime.room_id)
        orchestrator = self._unique_orchestrator(room.members)
        current_pending = self._pending_events(orchestrator.member_id)
        current_pending = tuple(
            event
            for event in current_pending
            if event.message.task_id == entry.task_id
            and event.message.trace_id == entry.runtime.task.trace_id
        )
        pending_ids = {event.message.message_id for event in current_pending}
        if any(event.message.message_id not in pending_ids for event in entry.pending_events):
            raise RecoveryCoordinatorError(
                "one or more pending events were acknowledged after recovery scan"
            )
        if self._ambiguous_pending_event_ids(
            entry.task_id,
            entry.runtime.task.trace_id,
            current_pending,
        ):
            raise RecoveryCoordinatorError(
                "pending events acquired workflow decisions after recovery scan"
            )
        result = await self.event_loop.run(entry.runtime, current_pending)

        task_revision = snapshot.revision
        if entry.runtime.task != snapshot.task:
            task_revision = self.tasks.save(
                entry.runtime.task,
                expected_revision=snapshot.revision,
            ).revision
        context_revision = self.contexts.save(
            entry.runtime.to_context(),
            expected_revision=context_snapshot.revision,
        ).revision
        return RecoveryRun(
            entry=entry,
            workflow_result=result,
            task_revision=task_revision,
            runtime_revision=context_revision,
        )

    async def recover_startup(self, *, page_size: int = 100) -> StartupRecoveryReport:
        """Scan tasks and resume only entries explicitly classified as safe."""
        entries = await self.scan(page_size=page_size)
        resumed: list[RecoveryRun] = []
        failures: list[tuple[UUID, str]] = []
        for entry in entries:
            if entry.disposition is not RecoveryDisposition.RESUMABLE:
                continue
            try:
                resumed.append(await self.resume(entry))
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
                reason = f"workflow resume failed and requires review: {type(exc).__name__}: {exc}"
                failures.append((entry.task_id, reason))
                self._needs_human(self.tasks.get(entry.task_id), reason)
        return StartupRecoveryReport(
            entries=entries,
            resumed=tuple(resumed),
            failures=tuple(failures),
        )

    async def _inspect_task(self, snapshot: TaskSnapshot) -> RecoveryEntry:
        task = snapshot.task
        if task.state is TaskState.NEEDS_HUMAN:
            return self._record(
                RecoveryEntry(
                    task_id=task.id,
                    disposition=RecoveryDisposition.NEEDS_HUMAN,
                    reason="task is awaiting human input",
                    task_revision=snapshot.revision,
                )
            )
        if task.state in TERMINAL_STATES:
            return self._record(
                RecoveryEntry(
                    task_id=task.id,
                    disposition=RecoveryDisposition.TERMINAL,
                    reason=f"task is already {task.state.value}",
                    task_revision=snapshot.revision,
                )
            )

        try:
            self._require_no_continuation_reservation(task.id)
            context_snapshot = self.contexts.get(task.id)
            context = context_snapshot.context
            if context.trace_id != task.trace_id:
                raise RecoveryCoordinatorError("runtime context trace ID does not match task")
            room = self.rooms.get_room(context.room_id)
            if room.task_id != task.id or room.trace_id != task.trace_id:
                raise RecoveryCoordinatorError("TeamRoom identity does not match task")
            if room.status.value != "active":
                raise RecoveryCoordinatorError("TeamRoom is closed")
            handle = await self.worktrees.inspect(task.id)
            if handle != context.worktree:
                raise RecoveryCoordinatorError(
                    "managed Worktree does not match the persisted runtime context"
                )
            runtime = WorkflowRuntime.from_context(task, context)
            self.evidence.restore_runtime(runtime)
            orchestrator = self._unique_orchestrator(room.members)
            pending = self._pending_events(orchestrator.member_id)
            if any(
                item.message.task_id != task.id
                or item.message.trace_id != task.trace_id
                or item.message.room_id != room.room_id
                for item in pending
            ):
                raise RecoveryCoordinatorError("pending TeamRoom event identity mismatch")
            ambiguous = self._ambiguous_pending_event_ids(task.id, task.trace_id, pending)
            if ambiguous:
                raise RecoveryCoordinatorError(
                    "pending events have workflow decisions already recorded; "
                    "their directive execution outcome is ambiguous"
                )
            if pending:
                return self._record(
                    RecoveryEntry(
                        task_id=task.id,
                        disposition=RecoveryDisposition.RESUMABLE,
                        reason=f"{len(pending)} unprocessed orchestrator event(s) are pending",
                        task_revision=snapshot.revision,
                        runtime_revision=context_snapshot.revision,
                        runtime=runtime,
                        pending_events=pending,
                    )
                )
            return self._record(
                RecoveryEntry(
                    task_id=task.id,
                    disposition=RecoveryDisposition.WAITING,
                    reason="runtime, evidence, room, and Worktree are valid; no orchestrator event is pending",
                    task_revision=snapshot.revision,
                    runtime_revision=context_snapshot.revision,
                    runtime=runtime,
                )
            )
        except RuntimeContextNotFoundError as exc:
            return self._needs_human(snapshot, f"runtime context is missing: {exc}")
        except (
            EvidenceRecoveryError,
            MemberNotFoundError,
            RoomNotFoundError,
            RecoveryCoordinatorError,
            RuntimeContextIntegrityError,
            WorktreeError,
            ContinuationIntegrityError,
        ) as exc:
            return self._needs_human(snapshot, str(exc))

    def _require_no_continuation_reservation(self, task_id: UUID) -> None:
        # Standalone legacy recovery stores may not install continuation tables.
        # Never reinterpret staged/uncertain Human execution as an ordinary
        # replayable controller event, including between scan and resume.
        with self.tasks.database.connect() as connection:
            if connection.execute("SELECT name FROM sqlite_master WHERE name='continuation_requests'").fetchone() is None:
                return
            rows = connection.execute("SELECT * FROM continuation_requests WHERE task_id=?",
                                      (str(task_id),)).fetchall()
            records = [ContinuationRepository(self.tasks.database)._decode(row) for row in rows]
            if any(record.receipt.state is not ContinuationState.SUCCEEDED for record in records):
                raise RecoveryCoordinatorError("unresolved continuation reservation; automatic recovery is forbidden")

    def _needs_human(self, snapshot: TaskSnapshot, reason: str) -> RecoveryEntry:
        task = snapshot.task
        if task.state in TERMINAL_STATES and task.state is not TaskState.NEEDS_HUMAN:
            return self._record(
                RecoveryEntry(
                    task_id=task.id,
                    disposition=RecoveryDisposition.TERMINAL,
                    reason=f"task became terminal during recovery: {reason}",
                    task_revision=snapshot.revision,
                )
            )
        if task.state is not TaskState.NEEDS_HUMAN:
            try:
                task.transition_to(TaskState.NEEDS_HUMAN)
            except ValueError:
                # Keep the original state if the state machine does not allow escalation.
                pass
            else:
                saved = self.tasks.save(task, expected_revision=snapshot.revision)
                self.traces.append(
                    TraceEvent(
                        task_id=task.id,
                        trace_id=task.trace_id,
                        type=TraceEventType.TASK_STATE_CHANGED,
                        actor_kind=TraceActorKind.DETERMINISTIC,
                        actor_id="workflow_recovery_coordinator",
                        idempotency_key=f"recovery-task-state:{task.id}:{saved.revision}",
                        occurred_at=task.updated_at,
                        payload={
                            "from": snapshot.task.state.value,
                            "to": TaskState.NEEDS_HUMAN.value,
                            "path": [TaskState.NEEDS_HUMAN.value],
                        },
                    )
                )
        return self._record(
            RecoveryEntry(
                task_id=task.id,
                disposition=RecoveryDisposition.NEEDS_HUMAN,
                reason=reason,
                task_revision=self.tasks.get(task.id).revision,
            )
        )

    def _record(self, entry: RecoveryEntry) -> RecoveryEntry:
        task = self.tasks.get(entry.task_id).task
        event_identity = "|".join(
            (
                entry.disposition.value,
                entry.reason,
                *(str(item.message.message_id) for item in entry.pending_events),
            )
        )
        fingerprint = hashlib.sha256(event_identity.encode("utf-8")).hexdigest()
        self.traces.append(
            TraceEvent(
                task_id=entry.task_id,
                trace_id=self.tasks.get(entry.task_id).task.trace_id,
                type=TraceEventType.RECOVERY_DECIDED,
                actor_kind=TraceActorKind.DETERMINISTIC,
                actor_id="workflow_recovery_coordinator",
                occurred_at=task.updated_at,
                idempotency_key=(f"recovery:{entry.task_id}:{entry.task_revision}:{fingerprint}"),
                payload={
                    "disposition": entry.disposition.value,
                    "reason": entry.reason,
                    "task_revision": entry.task_revision,
                    "pending_message_ids": [
                        str(item.message.message_id) for item in entry.pending_events
                    ],
                },
            )
        )
        return entry

    def _pending_events(self, orchestrator_id: UUID) -> tuple[StoredChatMessage, ...]:
        events: list[StoredChatMessage] = []
        cursor = 0
        while True:
            page = self.rooms.pending_for(
                orchestrator_id,
                after_sequence=cursor,
                limit=500,
            )
            if not page:
                return tuple(events)
            events.extend(page)
            cursor = page[-1].sequence

    def _ambiguous_pending_event_ids(
        self,
        task_id: UUID,
        trace_id: UUID,
        pending: tuple[StoredChatMessage, ...],
    ) -> set[UUID]:
        pending_ids = {item.message.message_id for item in pending}
        if not pending_ids:
            return set()
        ambiguous: set[UUID] = set()
        cursor = 0
        while True:
            page = self.traces.list(
                task_id=task_id,
                trace_id=trace_id,
                type=TraceEventType.WORKFLOW_DECISION,
                after_sequence=cursor,
                limit=500,
            )
            if not page:
                return ambiguous
            for stored in page:
                message_id = stored.event.payload.get("message_id")
                if isinstance(message_id, str):
                    try:
                        candidate = UUID(message_id)
                    except ValueError:
                        continue
                    if candidate in pending_ids:
                        ambiguous.add(candidate)
            cursor = page[-1].sequence

    @staticmethod
    def _unique_orchestrator(members):
        matches = tuple(member for member in members if member.role is MemberRole.ORCHESTRATOR)
        if len(matches) != 1:
            raise RecoveryCoordinatorError(
                f"expected exactly one orchestrator member, found {len(matches)}"
            )
        return matches[0]


class RecoveryCoordinatorError(RuntimeError):
    """Raised when a recovered workflow cannot be resumed safely."""
