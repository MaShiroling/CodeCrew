"""Cancellation intent and adapter observations, never proof of all processes stopping."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentExitReason
from app.orchestration.models import TaskState, utc_now
from app.storage import ArtifactReference, Migration
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationRepository,
    ContinuationState,
)
from app.storage.runtime import RuntimeContextRepository
from app.storage.tasks import TaskRepository
from app.trace import TraceActorKind, TraceEvent, TraceEventType


class CancelContinuationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_runtime_revision: int = Field(ge=1, strict=True)
    expected_claim_updated_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=1000)


class CancellationObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["adapter_terminal_result", "no_session", "cleanup_failed",
                     "cleanup_timed_out", "invalid_result", "no_observation"]
    session_id: UUID | None = None
    result_artifact: ArtifactReference | None = None
    exit_reason: AgentExitReason | None = None
    exit_code: int | None = None
    observed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_evidence(self):
        if self.outcome == "adapter_terminal_result":
            if self.session_id is None or self.result_artifact is None or self.exit_reason is None:
                raise ValueError("terminal observation requires session/result evidence")
        elif self.result_artifact is not None or self.exit_reason is not None or self.exit_code is not None:
            raise ValueError("unknown stop cannot claim a terminal result")
        return self


class ContinuationCancellationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    task_id: UUID
    trace_id: UUID
    human_member_id: UUID
    command: CancelContinuationCommand
    state: Literal["requested", "observed"] = "requested"
    observation: CancellationObservation | None = None
    external_process_stopped_confirmed: Literal[False] = False
    claim_released: Literal[False] = False
    budget_reset: Literal[False] = False
    task_completion_evaluated: Literal[False] = False
    requested_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self):
        if (self.state == "observed") != (self.observation is not None):
            raise ValueError("observed cancellation requires an observation")
        return self


CANCELLATION_MIGRATIONS = (Migration(
    version=12, name="create_continuation_cancellations", statements=(
        """CREATE TABLE continuation_cancellations (
        request_id TEXT PRIMARY KEY REFERENCES continuation_requests(request_id),
        task_id TEXT NOT NULL, trace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, owner_token TEXT NOT NULL,
        receipt_json TEXT NOT NULL, UNIQUE(task_id, idempotency_key))""",
    ),
),)


class ContinuationCancellationRepository:
    def __init__(self, claims: ContinuationRepository):
        self.claims = claims
        self.database = claims.database

    def initialize(self):
        self.database.initialize(CANCELLATION_MIGRATIONS)

    def get(self, *, task_id, request_id):
        with self.database.connect() as connection:
            record = self.claims._get_scoped(connection, task_id, request_id)
            row = connection.execute(
                "SELECT * FROM continuation_cancellations WHERE request_id=?", (str(request_id),),
            ).fetchone()
            return self._bound(record, row) if row is not None else None

    def request(self, *, task_id, request_id, human_member_id, command, local_claim):
        command = CancelContinuationCommand.model_validate_json(command.model_dump_json())
        with self.database.transaction() as connection:
            record = self.claims._get_scoped(connection, task_id, request_id)
            rows = connection.execute(
                "SELECT * FROM continuation_cancellations WHERE task_id=? AND (idempotency_key=? OR request_id=?)",
                (str(task_id), str(command.idempotency_key), str(request_id)),
            ).fetchall()
            if rows:
                receipt = self._decode(rows[0])
                if (len(rows) != 1 or receipt.request_id != request_id
                        or receipt.human_member_id != human_member_id or receipt.command != command):
                    raise ContinuationConflictError("cancellation intent conflicts with stored request")
                self._bound(record, rows[0])
                return receipt, False
            if (local_claim is None or record.receipt.state is not ContinuationState.CLAIMED
                    or record.claim_token != local_claim.claim_token
                    or record.receipt.request != local_claim.receipt.request
                    or record.updated_at != command.expected_claim_updated_at):
                raise ContinuationConflictError("cancellation requires this service's active owned claim")
            intent = record.receipt.request
            task_row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
            runtime_row = connection.execute("SELECT * FROM workflow_runtime_contexts WHERE task_id=?", (str(task_id),)).fetchone()
            if task_row is None or runtime_row is None:
                raise ContinuationConflictError("cancellation task/runtime unavailable")
            task = TaskRepository._snapshot_from_row(task_row)
            runtime = RuntimeContextRepository._snapshot_from_row(runtime_row)
            humans = connection.execute(
                "SELECT * FROM room_members WHERE room_id=? AND role='human'", (str(intent.room_id),),
            ).fetchall()
            room = connection.execute("SELECT * FROM team_rooms WHERE room_id=?", (str(intent.room_id),)).fetchone()
            if (task.revision != command.expected_revision
                    or runtime.revision != command.expected_runtime_revision
                    or task.task.state is not TaskState.NEEDS_HUMAN or task.task.trace_id != intent.trace_id
                    or runtime.context.trace_id != intent.trace_id or runtime.context.room_id != intent.room_id
                    or room is None or (room["task_id"], room["trace_id"], room["status"]) != (
                        str(task_id), str(intent.trace_id), "active")
                    or len(humans) != 1 or humans[0]["kind"] != "human"
                    or humans[0]["member_id"] != str(human_member_id)):
                raise ContinuationConflictError("cancellation scope, identity or revision changed")
            receipt = ContinuationCancellationReceipt(
                task_id=task_id, trace_id=intent.trace_id, request_id=request_id,
                human_member_id=human_member_id, command=command,
            )
            connection.execute("INSERT INTO continuation_cancellations VALUES (?, ?, ?, ?, ?, ?)", (
                str(request_id), str(task_id), str(intent.trace_id), str(command.idempotency_key),
                str(record.claim_token), receipt.model_dump_json(),
            ))
            self._trace(connection, record, receipt, TraceEventType.CONTINUATION_CANCEL_REQUESTED)
            return receipt, True

    def observe(self, claim, observation):
        observation = CancellationObservation.model_validate_json(observation.model_dump_json())
        with self.database.transaction() as connection:
            record = self.claims._get(connection, claim.receipt.request.request_id)
            row = connection.execute("SELECT * FROM continuation_cancellations WHERE request_id=?",
                                     (str(record.receipt.request.request_id),)).fetchone()
            if row is None:
                return None  # Direct coroutine cancellation has no Human request.
            receipt = self._bound(record, row)
            if (record.claim_token != claim.claim_token or row["owner_token"] != str(claim.claim_token)
                    or record.receipt.request != claim.receipt.request):
                raise ContinuationConflictError("cancellation observation owner does not match")
            if receipt.observation is not None:
                return receipt
            if observation.result_artifact is not None:
                reference = observation.result_artifact
                artifact = connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (str(reference.artifact_id),)).fetchone()
                if artifact is None or (artifact["task_id"], artifact["trace_id"], artifact["sha256"]) != (
                    str(receipt.task_id), str(receipt.trace_id), reference.sha256,
                ):
                    raise ContinuationConflictError("cancellation Artifact scope does not match")
                if reference.type.value != "generic" or artifact["artifact_type"] != "generic":
                    raise ContinuationConflictError("cancellation evidence is not a result Artifact")
            done = receipt.model_copy(update={"state": "observed", "observation": observation})
            connection.execute("UPDATE continuation_cancellations SET receipt_json=? WHERE request_id=?",
                               (done.model_dump_json(), str(receipt.request_id)))
            self._trace(connection, record, done, TraceEventType.CONTINUATION_CANCEL_OBSERVED)
            return done

    def _decode(self, row):
        try:
            receipt = ContinuationCancellationReceipt.model_validate_json(row["receipt_json"])
            if (str(receipt.request_id), str(receipt.task_id), str(receipt.trace_id),
                    str(receipt.command.idempotency_key), receipt.model_dump_json()) != tuple(row[key] for key in (
                        "request_id", "task_id", "trace_id", "idempotency_key", "receipt_json")):
                raise ValueError("cancellation indexed columns disagree with receipt")
            UUID(row["owner_token"])
            return receipt
        except (ValueError, TypeError) as exc:
            raise ContinuationIntegrityError("persisted cancellation receipt is corrupt") from exc

    def _bound(self, record, row):
        receipt = self._decode(row)
        intent = record.receipt.request
        if (receipt.request_id != intent.request_id or receipt.task_id != intent.task_id
                or receipt.trace_id != intent.trace_id or row["owner_token"] != str(record.claim_token)):
            raise ContinuationIntegrityError("cancellation does not match original claim scope/owner")
        return receipt

    def _trace(self, connection, record, receipt, type):
        intent = record.receipt.request
        observation = receipt.observation
        self.claims.traces.append_in_transaction(connection, TraceEvent(
            task_id=intent.task_id, trace_id=intent.trace_id, type=type,
            actor_kind=TraceActorKind.HUMAN if observation is None else TraceActorKind.DETERMINISTIC,
            actor_id=str(receipt.human_member_id) if observation is None else "continuation_cancellation",
            correlation_id=intent.correlation_id, causation_id=intent.message_id,
            idempotency_key=f"continuation-cancel:{intent.request_id}:{receipt.state}",
            payload={"request_id": str(intent.request_id), "state": receipt.state,
                     "outcome": observation.outcome if observation else None,
                     "reason": receipt.command.reason if observation is None else None,
                     "result_artifact_id": str(observation.result_artifact.artifact_id) if observation and observation.result_artifact else None,
                     "external_process_stopped_confirmed": False, "claim_released": False},
        ))
