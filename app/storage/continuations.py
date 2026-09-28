"""Durable at-most-once continuation claims, not exactly-once CLI execution."""

import hashlib
import json
import sqlite3
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentRole, AgentSession
from app.orchestration.models import Task, TaskState, utc_now
from app.storage.continuation_workflows import ContinuationWorkflowOutcome
from app.storage.runtime import RuntimeContextRepository, WorkflowRuntimeContext
from app.storage.sqlite import Migration, SQLiteDatabase
from app.storage.tasks import TaskRepository
from app.trace.models import TraceActorKind, TraceEvent, TraceEventType
from app.trace.store import TraceStore, _fingerprint


class ContinuationConflictError(RuntimeError):
    pass


class ContinuationIntegrityError(RuntimeError):
    pass


class ContinuationNotFoundError(ContinuationConflictError):
    pass


class ContinuationState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    NEEDS_HUMAN = "needs_human"


class ContinuationIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    idempotency_key: UUID
    task_id: UUID
    trace_id: UUID
    room_id: UUID
    message_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    correlation_id: UUID
    target_role: Literal[AgentRole.PLANNER, AgentRole.IMPLEMENTER, AgentRole.REVIEWER]
    target_member_id: UUID
    source_recipient_id: UUID
    agent_name: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1, strict=True)
    runtime_revision: int = Field(ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    def command(self) -> tuple:
        return (
            self.task_id,
            self.idempotency_key,
            self.message_id,
            self.target_role.value,
            self.expected_revision,
        )


class ContinuationReceipt(BaseModel):
    """SUCCEEDED describes one committed turn, never task completion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["single-agent-continuation", "controlled-workflow-continuation"] = "single-agent-continuation"
    request: ContinuationIntent
    state: ContinuationState
    task_state_at_request: Literal[TaskState.NEEDS_HUMAN] = TaskState.NEEDS_HUMAN
    task_completion_evaluated: Literal[False] = False
    runtime_revision: int | None = Field(default=None, ge=1, strict=True)
    agent_session_id: UUID | None = None
    output_message_ids: tuple[UUID, ...] = ()
    consumed_message_ids: tuple[UUID, ...] = ()
    failure_code: Literal["execution_failed", "cancelled", "budget_blocked"] | None = None
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.state is ContinuationState.SUCCEEDED:
            if (
                self.agent_session_id is None
                or self.runtime_revision != self.request.runtime_revision + 1
                or self.finished_at is None
                or self.failure_code is not None
                or self.request.message_id not in self.consumed_message_ids
            ):
                raise ValueError(
                    "successful turn requires committed session, runtime and input consumption"
                )
        elif self.state is ContinuationState.NEEDS_HUMAN and (
            self.failure_code is None or self.finished_at is None
        ):
            raise ValueError("paused continuation requires a failure code and timestamp")
        if self.state is not ContinuationState.SUCCEEDED and (
            self.agent_session_id is not None
            or self.runtime_revision is not None
            or self.output_message_ids
            or self.consumed_message_ids
        ):
            raise ValueError("uncommitted turn cannot claim committed output or consumption")
        if self.state in {ContinuationState.PENDING, ContinuationState.CLAIMED} and (
            self.finished_at is not None or self.failure_code is not None
        ):
            raise ValueError("unresolved continuation cannot have a terminal receipt")
        return self


class ContinuationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: ContinuationReceipt
    claim_token: UUID | None = None
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_claim(self):
        if (self.receipt.state is ContinuationState.PENDING) != (self.claim_token is None):
            raise ValueError("only pending requests lack a claim token")
        return self


class QuarantineContinuationCommand(BaseModel):
    """Human containment decision, not a stop confirmation or retry permit."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_runtime_revision: int = Field(ge=1, strict=True)
    expected_claim_updated_at: AwareDatetime
    disposition: Literal["quarantine"] = "quarantine"
    reason: str = Field(min_length=1, max_length=1_000)


class ContinuationQuarantineReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution_id: UUID = Field(default_factory=uuid4)
    command: QuarantineContinuationCommand
    task_id: UUID
    trace_id: UUID
    room_id: UUID
    request_id: UUID
    human_member_id: UUID
    observed_state: Literal[ContinuationState.CLAIMED, ContinuationState.NEEDS_HUMAN]
    claim_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_state_at_resolution: Literal[TaskState.NEEDS_HUMAN] = TaskState.NEEDS_HUMAN
    claim_released: Literal[False] = False
    external_process_stopped_confirmed: Literal[False] = False
    agent_dispatched: Literal[False] = False
    budget_reset: Literal[False] = False
    task_completion_evaluated: Literal[False] = False
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ContinuationStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: ContinuationReceipt
    updated_at: AwareDatetime
    task_revision: int = Field(ge=1, strict=True)
    runtime_revision: int = Field(ge=1, strict=True)
    task_state: TaskState
    quarantine: ContinuationQuarantineReceipt | None = None


CONTINUATION_MIGRATIONS = (
    Migration(
        version=10,
        name="create_continuation_requests",
        statements=(
            """CREATE TABLE continuation_requests (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            trace_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            message_id TEXT NOT NULL REFERENCES chat_messages(message_id),
            state TEXT NOT NULL CHECK(state IN ('pending','claimed','succeeded','needs_human')),
            claim_token TEXT,
            record_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, idempotency_key), UNIQUE(task_id, message_id)
        )""",
            """CREATE UNIQUE INDEX continuation_task_busy_idx ON continuation_requests(task_id)
        WHERE state IN ('pending','claimed','needs_human')""",
        ),
    ),
    Migration(
        version=11,
        name="create_continuation_quarantines",
        statements=(
            """CREATE TABLE continuation_quarantines (
            request_id TEXT PRIMARY KEY REFERENCES continuation_requests(request_id),
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            trace_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            human_member_id TEXT NOT NULL REFERENCES room_members(member_id),
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, idempotency_key)
        )""",
        ),
    ),
)


class ContinuationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.traces = TraceStore(database)

    def initialize(self) -> None:
        self.traces.initialize()
        self.database.initialize(CONTINUATION_MIGRATIONS)

    def get_by_key(self, task_id: UUID, key: UUID) -> ContinuationRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM continuation_requests WHERE task_id=? AND idempotency_key=?",
                (str(task_id), str(key)),
            ).fetchone()
            return self._decode(row) if row is not None else None

    def get(self, request_id: UUID) -> ContinuationRecord:
        with self.database.connect() as connection:
            return self._get(connection, request_id)

    def get_scoped(self, *, task_id: UUID, request_id: UUID) -> ContinuationRecord:
        with self.database.connect() as connection:
            return self._get_scoped(connection, task_id, request_id)

    def status(self, *, task_id: UUID, request_id: UUID) -> ContinuationStatus:
        with self.database.transaction(immediate=False) as connection:
            record = self._get_scoped(connection, task_id, request_id)
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (str(task_id),),
            ).fetchone()
            context_row = connection.execute(
                "SELECT * FROM workflow_runtime_contexts WHERE task_id=?", (str(task_id),),
            ).fetchone()
            if task_row is None or context_row is None:
                raise ContinuationConflictError("continuation task/runtime unavailable")
            task = TaskRepository._snapshot_from_row(task_row)
            context = RuntimeContextRepository._snapshot_from_row(context_row)
            intent = record.receipt.request
            if (task.task.trace_id != intent.trace_id or context.context.trace_id != intent.trace_id
                    or context.context.room_id != intent.room_id):
                raise ContinuationIntegrityError("continuation task/runtime scope is inconsistent")
            quarantine = self._quarantine_for(connection, request_id)
            if quarantine is not None:
                self._validate_quarantine_binding(record, quarantine)
            return ContinuationStatus(
                receipt=record.receipt, updated_at=record.updated_at, quarantine=quarantine,
                task_revision=task.revision, runtime_revision=context.revision,
                task_state=task.task.state,
            )

    def quarantine(
        self, *, task_id: UUID, request_id: UUID, human_member_id: UUID,
        command: QuarantineContinuationCommand,
    ) -> ContinuationQuarantineReceipt:
        """Atomically fence commits and audit Human intent; never free the task slot."""
        command = QuarantineContinuationCommand.model_validate_json(command.model_dump_json())
        with self.database.transaction() as connection:
            record = self._get_scoped(connection, task_id, request_id)
            existing = connection.execute(
                "SELECT * FROM continuation_quarantines WHERE task_id=? AND idempotency_key=?",
                (str(task_id), str(command.idempotency_key)),
            ).fetchone()
            if existing is not None:
                receipt = self._decode_quarantine(existing)
                if (receipt.request_id != request_id or receipt.human_member_id != human_member_id
                        or receipt.command != command):
                    raise ContinuationConflictError("quarantine idempotency key conflicts")
                self._validate_quarantine_binding(record, receipt)
                return receipt
            if self._quarantine_for(connection, request_id) is not None:
                raise ContinuationConflictError("continuation is already quarantined")
            if (record.receipt.state not in {ContinuationState.CLAIMED, ContinuationState.NEEDS_HUMAN}
                    or record.updated_at != command.expected_claim_updated_at):
                raise ContinuationConflictError("continuation state or timestamp changed")
            intent = record.receipt.request
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (str(task_id),),
            ).fetchone()
            context_row = connection.execute(
                "SELECT * FROM workflow_runtime_contexts WHERE task_id=?", (str(task_id),),
            ).fetchone()
            if task_row is None or context_row is None:
                raise ContinuationConflictError("quarantine task/runtime unavailable")
            task = TaskRepository._snapshot_from_row(task_row)
            context = RuntimeContextRepository._snapshot_from_row(context_row)
            if (task.revision != command.expected_revision
                    or context.revision != command.expected_runtime_revision
                    or task.task.state is not TaskState.NEEDS_HUMAN
                    or task.task.trace_id != intent.trace_id
                    or context.context.trace_id != intent.trace_id
                    or context.context.room_id != intent.room_id):
                raise ContinuationConflictError("quarantine task/runtime scope or revision changed")
            room = connection.execute(
                "SELECT * FROM team_rooms WHERE room_id=?", (str(intent.room_id),),
            ).fetchone()
            humans = connection.execute(
                "SELECT * FROM room_members WHERE room_id=? AND role='human'",
                (str(intent.room_id),),
            ).fetchall()
            if (room is None or (room["task_id"], room["trace_id"], room["status"]) != (
                str(task_id), str(intent.trace_id), "active",
            ) or len(humans) != 1 or humans[0]["kind"] != "human"
                    or humans[0]["member_id"] != str(human_member_id)):
                raise ContinuationConflictError("quarantine requires the room's unique Human identity")
            receipt = ContinuationQuarantineReceipt(
                command=command, task_id=task_id, trace_id=intent.trace_id,
                room_id=intent.room_id, request_id=request_id, human_member_id=human_member_id,
                observed_state=record.receipt.state,
                claim_record_sha256=self._record_digest(record),
            )
            connection.execute(
                "INSERT INTO continuation_quarantines VALUES (?, ?, ?, ?, ?, ?, ?)",
                self._quarantine_columns(receipt),
            )
            self.traces.append_in_transaction(connection, TraceEvent(
                task_id=task_id, trace_id=intent.trace_id,
                type=TraceEventType.CONTINUATION_QUARANTINED,
                actor_kind=TraceActorKind.HUMAN, actor_id=str(human_member_id),
                correlation_id=intent.correlation_id, causation_id=intent.message_id,
                idempotency_key=f"continuation-quarantine:{request_id}",
                payload={"resolution_id": str(receipt.resolution_id),
                         "request_id": str(request_id), "observed_state": receipt.observed_state.value,
                         "reason": command.reason, "claim_released": False,
                         "external_process_stopped_confirmed": False,
                         "claim_record_sha256": receipt.claim_record_sha256},
            ))
            return receipt

    def active_for_task(self, task_id: UUID) -> ContinuationRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM continuation_requests WHERE task_id=? AND state!='succeeded'",
                (str(task_id),),
            ).fetchone()
            return self._decode(row) if row is not None else None

    @staticmethod
    def require_same_command(
        record, *, task_id, key, message_id, target_role, expected_revision
    ) -> None:
        command = (task_id, key, message_id, target_role.value, expected_revision)
        if record.receipt.request.command() != command:
            raise ContinuationConflictError(
                "continuation idempotency key was used for different intent"
            )

    def register(self, intent: ContinuationIntent) -> ContinuationRecord:
        intent = ContinuationIntent.model_validate_json(intent.model_dump_json())
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM continuation_requests WHERE task_id=? AND idempotency_key=?",
                (str(intent.task_id), str(intent.idempotency_key)),
            ).fetchone()
            if existing is not None:
                record = self._decode(existing)
                if record.receipt.request.command() != intent.command():
                    raise ContinuationConflictError("continuation key conflicts with stored intent")
                return record
            self._validate_current(connection, intent)
            record = ContinuationRecord(
                receipt=ContinuationReceipt(request=intent, state=ContinuationState.PENDING)
            )
            try:
                connection.execute(
                    """INSERT INTO continuation_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._columns(record),
                )
            except sqlite3.IntegrityError as exc:
                raise ContinuationConflictError(
                    "task or Human message already has a continuation reservation"
                ) from exc
            self._trace(connection, record, TraceEventType.CONTINUATION_REQUESTED)
            return record

    def http_replay(self, record: ContinuationRecord, command_sha256: str, *,
                    scope: str = "single-agent-continuation") -> bool:
        """Only replay HTTP admissions, never adopt an internal/unknown owner."""
        if record.receipt.scope != scope:
            raise ContinuationConflictError("HTTP continuation mode differs from admission")
        with self.database.connect() as connection:
            intent = record.receipt.request
            row = connection.execute(
                "SELECT * FROM trace_events WHERE trace_id=? AND idempotency_key=?",
                (str(intent.trace_id), f"continuation-http:{intent.request_id}"),
            ).fetchone()
            if row is None:
                raise ContinuationConflictError("continuation was not admitted through HTTP; owner cannot be adopted")
            try:
                event = TraceEvent.model_validate_json(row["event_json"])
                expected = self._http_event(record, command_sha256, scope=scope)
                if event.model_dump(exclude={"event_id", "occurred_at"}) != expected.model_dump(exclude={"event_id", "occurred_at"}):
                    raise ContinuationConflictError("HTTP continuation command conflicts with admission")
                if (row["event_fingerprint"] != _fingerprint(event)
                        or row["task_id"] != str(intent.task_id)
                        or row["trace_id"] != str(intent.trace_id)
                        or row["event_type"] != event.type.value
                        or row["event_id"] != str(event.event_id)):
                    raise ValueError("HTTP admission index is corrupt")
            except (ValueError, TypeError) as exc:
                raise ContinuationIntegrityError("HTTP admission is corrupt") from exc
            return True

    @staticmethod
    def _http_event(record, command_sha256, *, scope="single-agent-continuation"):
        intent = record.receipt.request
        return TraceEvent(
            task_id=intent.task_id, trace_id=intent.trace_id,
            type=TraceEventType.CONTINUATION_EXECUTION_ACCEPTED,
            actor_kind=TraceActorKind.DETERMINISTIC, actor_id="continuation_http",
            correlation_id=intent.correlation_id, causation_id=intent.message_id,
            idempotency_key=f"continuation-http:{intent.request_id}",
            payload={"request_id": str(intent.request_id), "command_sha256": command_sha256,
                     "scope": scope, "task_completion_evaluated": False},
        )

    def admit_first(self, intent: ContinuationIntent, *, command_sha256: str,
                    scope: str = "single-agent-continuation") -> ContinuationRecord:
        """First Human follow-up: atomic admission/claim/trace, no bootstrap turn."""
        intent = ContinuationIntent.model_validate_json(intent.model_dump_json())
        with self.database.transaction() as connection:
            if connection.execute("SELECT request_id FROM continuation_requests WHERE task_id=?",
                                  (str(intent.task_id),)).fetchone() is not None:
                raise ContinuationConflictError("subsequent continuation requires a new intent authorization")
            self._validate_current(connection, intent)
            claim = ContinuationRecord(
                receipt=ContinuationReceipt(request=intent, state=ContinuationState.CLAIMED, scope=scope),
                claim_token=uuid4(),
            )
            connection.execute("INSERT INTO continuation_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self._columns(claim))
            pending = ContinuationRecord(receipt=ContinuationReceipt(request=intent, state=ContinuationState.PENDING, scope=scope))
            self._trace(connection, pending, TraceEventType.CONTINUATION_REQUESTED)
            self._trace(connection, claim, TraceEventType.CONTINUATION_CLAIMED)
            self.traces.append_in_transaction(connection, self._http_event(claim, command_sha256, scope=scope))
            return claim

    def workflow_outcome(self, *, task_id: UUID, request_id: UUID) -> ContinuationWorkflowOutcome | None:
        """Read the atomic, fingerprint-checked workflow result; never infer from prose."""
        with self.database.connect() as connection:
            record = self._get_scoped(connection, task_id, request_id)
            if record.receipt.state is not ContinuationState.SUCCEEDED:
                return None
            row = connection.execute(
                "SELECT * FROM trace_events WHERE trace_id=? AND idempotency_key=?",
                (str(record.receipt.request.trace_id), f"continuation-workflow:{request_id}"),
            ).fetchone()
            if row is None:
                return None
            try:
                event = TraceEvent.model_validate_json(row["event_json"])
                if (row["event_fingerprint"] != _fingerprint(event)
                        or row["event_id"] != str(event.event_id)
                        or row["task_id"] != str(task_id)
                        or row["trace_id"] != str(event.trace_id)
                        or row["event_type"] != event.type.value
                        or event.type is not TraceEventType.CONTINUATION_WORKFLOW_FINISHED):
                    raise ValueError("workflow result trace index differs")
                outcome = ContinuationWorkflowOutcome.model_validate(event.payload["outcome"])
                if (outcome.request_id != request_id or outcome.task_id != task_id
                        or outcome.trace_id != record.receipt.request.trace_id):
                    raise ValueError("workflow result belongs to another continuation")
                return outcome
            except (KeyError, TypeError, ValueError) as exc:
                raise ContinuationIntegrityError("workflow result trace is corrupt") from exc

    def claim(self, request_id: UUID) -> ContinuationRecord | None:
        """CAS once; claimed/terminal requests are NEVER re-leased on timeout."""
        with self.database.transaction() as connection:
            record = self._get(connection, request_id)
            if record.receipt.state is not ContinuationState.PENDING:
                return None
            self._validate_current(connection, record.receipt.request)
            claimed = record.model_copy(
                update={
                    "receipt": record.receipt.model_copy(
                        update={"state": ContinuationState.CLAIMED}
                    ),
                    "claim_token": uuid4(),
                    "updated_at": utc_now(),
                }
            )
            self._update(connection, claimed, expected=record)
            self._trace(connection, claimed, TraceEventType.CONTINUATION_CLAIMED)
            return claimed

    def finish(
        self,
        claim: ContinuationRecord,
        *,
        context: WorkflowRuntimeContext,
        session: AgentSession,
        input_ids: tuple[UUID, ...],
        output_ids: tuple[UUID, ...],
        park_resumed: bool = False,
        workflow_outcome: ContinuationWorkflowOutcome | None = None,
        final_task: Task | None = None,
    ) -> ContinuationReceipt:
        """Commit Runtime CAS + selected ACKs + receipt + trace atomically.

        Agent file writes, routed outputs and usage records are outside this
        transaction. If it fails, ownership remains consumed: never rerun CLI.
        """
        with self.database.transaction() as connection:
            record = self._owned(connection, claim)
            # Migration 12 is installed by the service; older standalone
            # repositories remain usable. A durable cancel fences success even
            # if an adapter swallowed coroutine cancellation.
            if connection.execute("SELECT name FROM sqlite_master WHERE name='continuation_cancellations'").fetchone() is not None and connection.execute(
                "SELECT request_id FROM continuation_cancellations WHERE request_id=?",
                (str(record.receipt.request.request_id),),
            ).fetchone() is not None:
                raise ContinuationConflictError("continuation cancellation requested; successful commit forbidden")
            intent = record.receipt.request
            expected_state = self._execution_state(connection, intent, park_resumed=park_resumed)
            persisted = self._validate_current(connection, intent, expected_state=expected_state)
            if (workflow_outcome is None) != (final_task is None):
                raise ContinuationConflictError("workflow outcome and final Task must be committed together")
            if workflow_outcome is not None:
                if record.receipt.scope != "controlled-workflow-continuation":
                    raise ContinuationConflictError("claim receipt is not a controlled workflow")
                admission = connection.execute(
                    "SELECT sequence,event_json,event_fingerprint FROM trace_events WHERE trace_id=? AND idempotency_key=?",
                    (str(intent.trace_id), f"continuation-http:{intent.request_id}"),
                ).fetchone()
                admission_event = TraceEvent.model_validate_json(admission["event_json"]) if admission else None
                if (admission_event is None
                        or admission["event_fingerprint"] != _fingerprint(admission_event)
                        or admission_event.payload.get("scope") != "controlled-workflow-continuation"):
                    raise ContinuationConflictError("claim was not admitted for a controlled workflow")
                if (workflow_outcome.request_id != intent.request_id
                        or workflow_outcome.task_id != intent.task_id
                        or workflow_outcome.trace_id != intent.trace_id):
                    raise ContinuationConflictError("workflow result has different scope")
                snapshot = TaskRepository._snapshot_from_row(connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (str(intent.task_id),),
                ).fetchone())
                TaskRepository._validate_immutable_fields(snapshot.task, final_task)
                if (final_task.state is not workflow_outcome.final_state
                        or final_task.rework_rounds != workflow_outcome.rework_rounds
                        or snapshot.revision != intent.expected_revision
                        or snapshot.task.state is not expected_state):
                    raise ContinuationConflictError("workflow final Task differs from claimed state")
                if workflow_outcome.success:
                    self._validate_workflow_guard_trace(connection, intent, workflow_outcome,
                                                        after_sequence=admission["sequence"])
            RuntimeContextRepository._validate_immutable_fields(persisted, context)
            if (
                session.task_id != intent.task_id
                or session.trace_id != intent.trace_id
                or session.role != intent.target_role
                or session.agent_name != intent.agent_name
            ):
                raise ContinuationConflictError("Agent session does not match claimed continuation")
            if context.verification_plan != persisted.verification_plan or {
                (b.role, b.agent_name) for b in context.agent_bindings
            } != {(b.role, b.agent_name) for b in persisted.agent_bindings}:
                raise ContinuationConflictError(
                    "continuation cannot change runtime policy or bindings"
                )
            handoff = connection.execute(
                "SELECT * FROM chat_messages WHERE room_id=? AND idempotency_key=?",
                (
                    str(intent.room_id),
                    f"human-continuation:{intent.message_id}:{intent.target_role.value}",
                ),
            ).fetchall()
            if len(handoff) != 1:
                raise ContinuationConflictError("continuation Handoff is missing or ambiguous")
            handoff_id = UUID(handoff[0]["message_id"])
            expected = {handoff_id}
            if intent.source_recipient_id == intent.target_member_id:
                expected.add(intent.message_id)
            if len(input_ids) != len(set(input_ids)) or set(input_ids) != expected:
                raise ContinuationConflictError(
                    "consumed input IDs do not match continuation selection"
                )
            acknowledgements = {message_id: intent.target_member_id for message_id in input_ids}
            acknowledgements[intent.message_id] = intent.source_recipient_id
            for message_id, recipient in acknowledgements.items():
                self._require_message_scope(connection, intent, message_id)
                cursor = connection.execute(
                    """UPDATE chat_deliveries SET status='acknowledged', acknowledged_at=?
                    WHERE message_id=? AND recipient_id=? AND status='pending'""",
                    (utc_now().isoformat(), str(message_id), str(recipient)),
                )
                if cursor.rowcount != 1:
                    raise ContinuationConflictError(
                        "continuation input delivery changed before commit"
                    )
            for message_id in output_ids:
                row = self._require_message_scope(connection, intent, message_id)
                if row["sender_id"] != str(intent.target_member_id):
                    raise ContinuationConflictError(
                        "continuation output is not from the target Agent"
                    )
            updated = context.model_copy(update={"updated_at": utc_now()})
            cursor = connection.execute(
                """UPDATE workflow_runtime_contexts SET context_json=?, revision=revision+1, updated_at=?
                WHERE task_id=? AND revision=?""",
                (
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    str(intent.task_id),
                    intent.runtime_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ContinuationConflictError(
                    "runtime revision changed before continuation commit"
                )
            receipt = ContinuationReceipt(
                scope=record.receipt.scope,
                request=intent,
                state=ContinuationState.SUCCEEDED,
                runtime_revision=intent.runtime_revision + 1,
                agent_session_id=session.session_id,
                output_message_ids=output_ids,
                consumed_message_ids=tuple(acknowledgements),
                finished_at=utc_now(),
            )
            done = record.model_copy(update={"receipt": receipt, "updated_at": utc_now()})
            self._update(connection, done, expected=record)
            self._trace(connection, done, TraceEventType.CONTINUATION_SUCCEEDED)
            if workflow_outcome is not None:
                cursor = connection.execute(
                    """UPDATE tasks SET state=?,rework_rounds=?,task_json=?,revision=revision+1,updated_at=?
                    WHERE task_id=? AND revision=?""",
                    (final_task.state.value, final_task.rework_rounds, final_task.model_dump_json(),
                     final_task.updated_at.isoformat(), str(intent.task_id), intent.expected_revision),
                )
                if cursor.rowcount != 1:
                    raise ContinuationConflictError("workflow Task revision changed before commit")
                self.traces.append_in_transaction(connection, TraceEvent(
                    task_id=intent.task_id, trace_id=intent.trace_id,
                    type=TraceEventType.CONTINUATION_WORKFLOW_FINISHED,
                    actor_kind=TraceActorKind.DETERMINISTIC, actor_id="continuation_workflow",
                    correlation_id=intent.correlation_id, causation_id=intent.message_id,
                    idempotency_key=f"continuation-workflow:{intent.request_id}",
                    payload={"outcome": workflow_outcome.model_dump(mode="json")},
                ))
            elif park_resumed:
                self._park_resumed_task(connection, intent, expected_state)
            return receipt

    @staticmethod
    def _validate_workflow_guard_trace(connection, intent, outcome, *, after_sequence):
        expected = (
            (TraceEventType.VERIFICATION_COMPLETED, outcome.verification_artifact_id),
            (TraceEventType.REVIEW_DECIDED, outcome.review_artifact_id),
            (TraceEventType.COMPLETION_DECIDED, outcome.completion_artifact_id),
        )
        previous = after_sequence
        for kind, artifact_id in expected:
            row = connection.execute(
                """SELECT * FROM trace_events WHERE trace_id=? AND event_type=? AND sequence>?
                ORDER BY sequence DESC LIMIT 1""",
                (str(intent.trace_id), kind.value, after_sequence),
            ).fetchone()
            if row is None or row["sequence"] <= previous:
                raise ContinuationConflictError("workflow completion lacks ordered fresh evidence")
            event = TraceEvent.model_validate_json(row["event_json"])
            if (row["event_fingerprint"] != _fingerprint(event)
                    or event.task_id != intent.task_id or event.trace_id != intent.trace_id):
                raise ContinuationIntegrityError("workflow evidence trace is corrupt")
            if kind is TraceEventType.REVIEW_DECIDED:
                valid = (event.payload.get("message_type") == "review_approved"
                         and str(artifact_id) in event.payload.get("artifact_ids", []))
            else:
                valid = (event.payload.get("artifact_id") == str(artifact_id)
                         and event.payload.get("passed") is True)
            if not valid:
                raise ContinuationConflictError("workflow evidence does not prove approval and passing guard")
            previous = row["sequence"]

    def pause(self, claim: ContinuationRecord, *, code: str, park_resumed: bool = False) -> ContinuationReceipt:
        with self.database.transaction() as connection:
            record = self._owned(connection, claim)
            if park_resumed:
                expected_state = self._execution_state(connection, record.receipt.request, park_resumed=True)
                self._park_resumed_task(connection, record.receipt.request, expected_state)
            receipt = ContinuationReceipt(
                scope=record.receipt.scope,
                request=record.receipt.request,
                state=ContinuationState.NEEDS_HUMAN,
                failure_code=code,
                finished_at=utc_now(),
            )
            paused = record.model_copy(update={"receipt": receipt, "updated_at": utc_now()})
            self._update(connection, paused, expected=record)
            self._trace(connection, paused, TraceEventType.CONTINUATION_PAUSED)
            return receipt

    def _execution_state(self, connection, intent, *, park_resumed):
        if not park_resumed:
            return TaskState.NEEDS_HUMAN
        # Active-state commits are restricted to a bound, consumed grant; do
        # not relax legacy pause-only claims or permit arbitrary active tasks.
        from app.storage.continuation_authorizations import ContinuationAuthorizationRepository
        from app.storage.continuation_resumptions import ContinuationResumptionRepository

        row = connection.execute("SELECT * FROM continuation_resumptions WHERE request_id=?",
                                 (str(intent.request_id),)).fetchone()
        if row is None:
            raise ContinuationConflictError("active continuation requires a consumed authorization")
        repository = ContinuationResumptionRepository(ContinuationAuthorizationRepository(self))
        grant = repository._grant(connection, intent.task_id, UUID(row["authorization_id"]))
        receipt = repository._bound(connection, grant, row)
        return receipt.resumed_state

    def _park_resumed_task(self, connection, intent, expected_state):
        snapshot = TaskRepository._snapshot_from_row(connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (str(intent.task_id),),
        ).fetchone())
        if (snapshot.revision != intent.expected_revision or snapshot.task.state is not expected_state
                or snapshot.task.trace_id != intent.trace_id):
            raise ContinuationConflictError("resumed task changed before parking")
        task = snapshot.task.model_copy(deep=True)
        task.transition_to(TaskState.NEEDS_HUMAN)
        cursor = connection.execute(
            "UPDATE tasks SET state=?,task_json=?,revision=revision+1,updated_at=? WHERE task_id=? AND revision=?",
            (task.state.value, task.model_dump_json(), task.updated_at.isoformat(), str(task.id), snapshot.revision),
        )
        if cursor.rowcount != 1:
            raise ContinuationConflictError("resumed task parking revision changed")
        self.traces.append_in_transaction(connection, TraceEvent(
            task_id=task.id, trace_id=task.trace_id, type=TraceEventType.TASK_STATE_CHANGED,
            actor_kind=TraceActorKind.DETERMINISTIC, actor_id="continuation_http",
            correlation_id=intent.correlation_id, causation_id=intent.message_id,
            idempotency_key=f"continuation-parked:{intent.request_id}",
            payload={"from": expected_state.value, "to": task.state.value, "task_completion_evaluated": False},
        ))

    def _validate_current(self, connection, intent, *, expected_state=TaskState.NEEDS_HUMAN) -> WorkflowRuntimeContext:
        task_row = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (str(intent.task_id),)
        ).fetchone()
        context_row = connection.execute(
            "SELECT * FROM workflow_runtime_contexts WHERE task_id=?",
            (str(intent.task_id),),
        ).fetchone()
        if task_row is None or context_row is None:
            raise ContinuationConflictError("continuation task/runtime no longer exists")
        task = TaskRepository._snapshot_from_row(task_row)
        context = RuntimeContextRepository._snapshot_from_row(context_row)
        if (
            task.revision != intent.expected_revision
            or task.task.state is not expected_state
            or task.task.trace_id != intent.trace_id
            or context.revision != intent.runtime_revision
            or context.context.trace_id != intent.trace_id
            or context.context.room_id != intent.room_id
        ):
            raise ContinuationConflictError("continuation task/runtime scope or revision changed")
        room = connection.execute(
            "SELECT * FROM team_rooms WHERE room_id=?", (str(intent.room_id),)
        ).fetchone()
        if room is None or (room["task_id"], room["trace_id"], room["status"]) != (
            str(intent.task_id),
            str(intent.trace_id),
            "active",
        ):
            raise ContinuationConflictError("continuation room is unavailable")
        target = connection.execute(
            "SELECT * FROM room_members WHERE member_id=?", (str(intent.target_member_id),)
        ).fetchone()
        if target is None or (target["room_id"], target["role"], target["kind"]) != (
            str(intent.room_id),
            intent.target_role.value,
            "agent",
        ):
            raise ContinuationConflictError("continuation target binding changed")
        bindings = [b for b in context.context.agent_bindings if b.role == intent.target_role]
        if len(bindings) != 1 or bindings[0].agent_name != intent.agent_name:
            raise ContinuationConflictError("continuation Agent name changed")
        source = self._require_message_scope(connection, intent, intent.message_id)
        human = connection.execute(
            "SELECT * FROM room_members WHERE member_id=?", (source["sender_id"],)
        ).fetchone()
        body = json.loads(source["message_json"])
        if human_message_digest(body) != intent.source_sha256:
            raise ContinuationConflictError("stored Human intent changed since preparation")
        if (
            human is None
            or human["role"] != "human"
            or human["kind"] != "human"
            or human["room_id"] != str(intent.room_id)
            or source["message_type"] not in {"message", "answer"}
            or not source["idempotency_key"].startswith("human-api:")
            or body.get("artifacts")
            or source["correlation_id"] != str(intent.correlation_id)
        ):
            raise ContinuationConflictError("continuation source is not the stored Human intent")
        deliveries = connection.execute(
            "SELECT * FROM chat_deliveries WHERE message_id=?", (str(intent.message_id),)
        ).fetchall()
        recipient = connection.execute(
            "SELECT * FROM room_members WHERE member_id=?", (str(intent.source_recipient_id),)
        ).fetchone()
        if (
            len(deliveries) != 1
            or deliveries[0]["status"] != "pending"
            or deliveries[0]["recipient_id"] != str(intent.source_recipient_id)
            or recipient is None
            or recipient["room_id"] != str(intent.room_id)
            or (
                intent.source_recipient_id != intent.target_member_id
                and (recipient["role"], recipient["kind"]) != ("orchestrator", "system")
            )
        ):
            raise ContinuationConflictError("Human intent delivery is no longer eligible")
        return context.context

    @staticmethod
    def _require_message_scope(connection, intent, message_id):
        row = connection.execute(
            "SELECT * FROM chat_messages WHERE message_id=?", (str(message_id),)
        ).fetchone()
        if row is None or (row["task_id"], row["trace_id"], row["room_id"]) != (
            str(intent.task_id),
            str(intent.trace_id),
            str(intent.room_id),
        ):
            raise ContinuationConflictError("continuation message belongs to another scope")
        return row

    def _owned(self, connection, claim):
        record = self._get(connection, claim.receipt.request.request_id)
        quarantine = self._quarantine_for(connection, record.receipt.request.request_id)
        if quarantine is not None:
            self._validate_quarantine_binding(record, quarantine)
            raise ContinuationConflictError("continuation is quarantined; late commit is forbidden")
        if (
            record.receipt.state is not ContinuationState.CLAIMED
            or claim.claim_token is None
            or record.claim_token != claim.claim_token
        ):
            raise ContinuationConflictError("continuation claim is not owned by this attempt")
        return record

    def _get(self, connection, request_id):
        row = connection.execute(
            "SELECT * FROM continuation_requests WHERE request_id=?", (str(request_id),)
        ).fetchone()
        if row is None:
            raise ContinuationNotFoundError("continuation request not found")
        return self._decode(row)

    def _get_scoped(self, connection, task_id, request_id):
        record = self._get(connection, request_id)
        if record.receipt.request.task_id != task_id:
            raise ContinuationNotFoundError("continuation request not found for this task")
        return record

    def _quarantine_for(self, connection, request_id):
        row = connection.execute(
            "SELECT * FROM continuation_quarantines WHERE request_id=?", (str(request_id),),
        ).fetchone()
        return self._decode_quarantine(row) if row is not None else None

    @staticmethod
    def _record_digest(record):
        return hashlib.sha256(record.model_dump_json().encode()).hexdigest()

    def _validate_quarantine_binding(self, record, receipt):
        intent = record.receipt.request
        if (receipt.request_id != intent.request_id or receipt.task_id != intent.task_id
                or receipt.trace_id != intent.trace_id or receipt.room_id != intent.room_id
                or receipt.observed_state != record.receipt.state
                or receipt.command.expected_claim_updated_at != record.updated_at
                or receipt.claim_record_sha256 != self._record_digest(record)):
            raise ContinuationIntegrityError("quarantine disagrees with its continuation snapshot")

    @staticmethod
    def _quarantine_columns(receipt):
        return (str(receipt.request_id), str(receipt.task_id), str(receipt.trace_id),
                str(receipt.command.idempotency_key), str(receipt.human_member_id),
                receipt.model_dump_json(), receipt.created_at.isoformat())

    def _decode_quarantine(self, row):
        try:
            receipt = ContinuationQuarantineReceipt.model_validate_json(row["receipt_json"])
            columns = tuple(row[key] for key in (
                "request_id", "task_id", "trace_id", "idempotency_key", "human_member_id",
                "receipt_json", "created_at",
            ))
            if columns != self._quarantine_columns(receipt):
                raise ValueError("quarantine indexed columns disagree with receipt")
            return receipt
        except (ValueError, TypeError) as exc:
            raise ContinuationIntegrityError("persisted quarantine receipt is corrupt") from exc

    @staticmethod
    def _columns(record):
        intent = record.receipt.request
        return (
            str(intent.request_id),
            str(intent.task_id),
            str(intent.trace_id),
            str(intent.idempotency_key),
            str(intent.message_id),
            record.receipt.state.value,
            str(record.claim_token) if record.claim_token else None,
            record.model_dump_json(),
            record.updated_at.isoformat(),
        )

    def _update(self, connection, record, *, expected):
        values = self._columns(record)
        cursor = connection.execute(
            """UPDATE continuation_requests SET state=?, claim_token=?, record_json=?, updated_at=?
            WHERE request_id=? AND state=? AND claim_token IS ?""",
            (*values[5:], values[0], expected.receipt.state.value,
             str(expected.claim_token) if expected.claim_token else None),
        )
        if cursor.rowcount != 1:
            raise ContinuationConflictError("continuation claim/state changed during compare-and-swap")

    def _decode(self, row):
        try:
            record = ContinuationRecord.model_validate_json(row["record_json"])
            columns = tuple(
                row[key]
                for key in (
                    "request_id",
                    "task_id",
                    "trace_id",
                    "idempotency_key",
                    "message_id",
                    "state",
                    "claim_token",
                    "record_json",
                    "updated_at",
                )
            )
            if columns != self._columns(record):
                raise ValueError("continuation indexed columns disagree with record")
            return record
        except (ValueError, TypeError) as exc:
            raise ContinuationIntegrityError("persisted continuation record is corrupt") from exc

    def _trace(self, connection, record, type):
        intent = record.receipt.request
        self.traces.append_in_transaction(
            connection,
            TraceEvent(
                task_id=intent.task_id,
                trace_id=intent.trace_id,
                type=type,
                actor_kind=TraceActorKind.DETERMINISTIC,
                actor_id="continuation_repository",
                correlation_id=intent.correlation_id,
                causation_id=intent.message_id,
                idempotency_key=f"continuation:{intent.request_id}:{record.receipt.state.value}",
                payload={
                    "request_id": str(intent.request_id),
                    "state": record.receipt.state.value,
                    "target_role": intent.target_role.value,
                    "failure_code": record.receipt.failure_code,
                    "agent_session_id": str(record.receipt.agent_session_id) if record.receipt.agent_session_id else None,
                    "runtime_revision": record.receipt.runtime_revision,
                    "output_message_ids": [str(item) for item in record.receipt.output_message_ids],
                },
            ),
        )


def human_message_digest(body: dict) -> str:
    """Bind the complete persisted Human envelope without copying its body."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
