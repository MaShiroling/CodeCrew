"""Durable at-most-once continuation claims, not exactly-once CLI execution."""

import hashlib
import json
import sqlite3
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentRole, AgentSession
from app.orchestration.models import TaskState, utc_now
from app.storage.runtime import RuntimeContextRepository, WorkflowRuntimeContext
from app.storage.sqlite import Migration, SQLiteDatabase
from app.storage.tasks import TaskRepository
from app.trace.models import TraceActorKind, TraceEvent, TraceEventType
from app.trace.store import TraceStore


class ContinuationConflictError(RuntimeError):
    pass


class ContinuationIntegrityError(RuntimeError):
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

    scope: Literal["single-agent-continuation"] = "single-agent-continuation"
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
    ) -> ContinuationReceipt:
        """Commit Runtime CAS + selected ACKs + receipt + trace atomically.

        Agent file writes, routed outputs and usage records are outside this
        transaction. If it fails, ownership remains consumed: never rerun CLI.
        """
        with self.database.transaction() as connection:
            record = self._owned(connection, claim)
            intent = record.receipt.request
            persisted = self._validate_current(connection, intent)
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
            return receipt

    def pause(self, claim: ContinuationRecord, *, code: str) -> ContinuationReceipt:
        with self.database.transaction() as connection:
            record = self._owned(connection, claim)
            receipt = ContinuationReceipt(
                request=record.receipt.request,
                state=ContinuationState.NEEDS_HUMAN,
                failure_code=code,
                finished_at=utc_now(),
            )
            paused = record.model_copy(update={"receipt": receipt, "updated_at": utc_now()})
            self._update(connection, paused, expected=record)
            self._trace(connection, paused, TraceEventType.CONTINUATION_PAUSED)
            return receipt

    def _validate_current(self, connection, intent) -> WorkflowRuntimeContext:
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
            or task.task.state is not TaskState.NEEDS_HUMAN
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
            raise ContinuationConflictError("continuation request not found")
        return self._decode(row)

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
