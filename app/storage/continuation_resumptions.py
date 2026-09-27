"""Consume a grant and stage recovery atomically, never dispatch or unlock an attempt."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentRole
from app.orchestration.models import TaskState, utc_now
from app.storage import ArtifactReference, ArtifactType, Migration
from app.storage.continuation_authorizations import ContinuationAuthorizationRepository
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationIntent,
    ContinuationNotFoundError,
    ContinuationReceipt,
    ContinuationRecord,
    ContinuationState,
    human_message_digest,
)
from app.storage.runtime import WorkflowRuntimeContext
from app.storage.tasks import TaskRepository, TaskSnapshot
from app.trace import TraceActorKind, TraceEvent, TraceEventType

RESUME_STATES = {
    AgentRole.PLANNER: TaskState.PLANNING,
    AgentRole.IMPLEMENTER: TaskState.IMPLEMENTING,
    AgentRole.REVIEWER: TaskState.VERIFYING,
}


class ContinuationResumptionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: UUID
    request_id: UUID
    task_id: UUID
    trace_id: UUID
    target_role: Literal[AgentRole.PLANNER, AgentRole.IMPLEMENTER, AgentRole.REVIEWER]
    from_state: Literal[TaskState.NEEDS_HUMAN] = TaskState.NEEDS_HUMAN
    resumed_state: Literal[TaskState.PLANNING, TaskState.IMPLEMENTING, TaskState.VERIFYING]
    task_revision: int = Field(ge=2, strict=True)
    runtime_revision: int = Field(ge=2, strict=True)
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_consumed: Literal[True] = True
    historical_evidence_invalidated: Literal[True] = True
    agent_dispatch_ready: Literal[False] = False
    agent_dispatched: Literal[False] = False
    claim_released: Literal[False] = False
    budget_reset: Literal[False] = False
    task_completion_evaluated: Literal[False] = False
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self):
        if self.resumed_state is not RESUME_STATES[self.target_role]:
            raise ValueError("role does not match the controlled resume state")
        return self


@dataclass(frozen=True)
class ClaimedResumption:
    receipt: ContinuationResumptionReceipt
    claim: ContinuationRecord | None = None
    task: TaskSnapshot | None = None
    context: WorkflowRuntimeContext | None = None
    replayed: bool = False


RESUMPTION_MIGRATIONS = (Migration(
    version=14, name="create_continuation_resumptions", statements=(
        """CREATE TABLE continuation_resumptions (
        authorization_id TEXT PRIMARY KEY REFERENCES continuation_authorizations(authorization_id),
        request_id TEXT NOT NULL UNIQUE REFERENCES continuation_requests(request_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        receipt_json TEXT NOT NULL)""",
    ),
),)


class ContinuationResumptionRepository:
    def __init__(self, authorizations: ContinuationAuthorizationRepository):
        self.authorizations = authorizations
        self.claims = authorizations.claims
        self.database = authorizations.database

    def initialize(self):
        self.database.initialize(RESUMPTION_MIGRATIONS)

    def get(self, *, task_id: UUID, authorization_id: UUID) -> ContinuationResumptionReceipt | None:
        with self.database.transaction(immediate=False) as connection:
            grant = self._grant(connection, task_id, authorization_id)
            row = connection.execute("SELECT * FROM continuation_resumptions WHERE authorization_id=?",
                                     (str(authorization_id),)).fetchone()
            return self._bound(connection, grant, row) if row is not None else None

    def consume(self, *, task_id: UUID, authorization_id: UUID,
                references: tuple[ArtifactReference, ...], validate_budget: Callable[[], None]) -> ClaimedResumption:
        """CAS grant + Task/Runtime + fresh claim + trace in one transaction.

        References and budget checks are supplied by the trusted preparation
        kernel, not by HTTP. Replays carry no owner token or executable runtime.
        """
        with self.database.transaction() as connection:
            grant = self._grant(connection, task_id, authorization_id)
            row = connection.execute("SELECT * FROM continuation_resumptions WHERE authorization_id=?",
                                     (str(authorization_id),)).fetchone()
            if row is not None:
                return ClaimedResumption(self._bound(connection, grant, row), replayed=True)
            intent = grant.intent
            if references != grant.artifacts:
                raise ContinuationConflictError("authorized evidence references changed")
            context = self.claims._validate_current(connection, intent)
            humans = connection.execute("SELECT * FROM room_members WHERE room_id=? AND role='human'",
                                        (str(intent.room_id),)).fetchall()
            if len(humans) != 1 or humans[0]["kind"] != "human" or humans[0]["member_id"] != str(grant.human_member_id):
                raise ContinuationConflictError("authorized Human identity changed")
            if human_message_digest(context.model_dump(mode="json")) != grant.runtime_sha256:
                raise ContinuationConflictError("authorized runtime changed")
            if connection.execute("SELECT request_id FROM continuation_requests WHERE task_id=? AND state!='succeeded'",
                                  (str(task_id),)).fetchone() is not None:
                raise ContinuationConflictError("task has an unresolved continuation reservation")
            for reference in references:
                artifact = connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (str(reference.artifact_id),)).fetchone()
                if artifact is None or (artifact["task_id"], artifact["trace_id"], artifact["sha256"], artifact["artifact_type"]) != (
                    str(task_id), str(intent.trace_id), reference.sha256, reference.type.value,
                ):
                    raise ContinuationConflictError("authorized Artifact binding changed")
            plan = connection.execute("SELECT * FROM plan_revisions WHERE room_id=? ORDER BY version DESC LIMIT 1",
                                      (str(intent.room_id),)).fetchone()
            plan_ids = [str(ref.artifact_id) for ref in references if ref.type is ArtifactType.PLAN]
            if plan_ids != ([plan["artifact_id"]] if plan is not None else []):
                raise ContinuationConflictError("authorized Plan version changed")
            validate_budget()  # No await; preserve accumulated attempts and rework.
            task_row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
            snapshot = TaskRepository._snapshot_from_row(task_row)
            restored_task = snapshot.task.model_copy(deep=True)
            restored_task.resume_for_continuation(RESUME_STATES[intent.target_role])
            restored_context = WorkflowRuntimeContext.model_validate_json(context.model_copy(update={
                "agent_bindings": tuple(binding.model_copy(update={"native_session_id": None}) for binding in context.agent_bindings),
                "updated_at": utc_now(),
            }).model_dump_json())
            restored_intent = ContinuationIntent.model_validate_json(intent.model_copy(update={
                "expected_revision": intent.expected_revision + 1,
                "runtime_revision": intent.runtime_revision + 1,
            }).model_dump_json())
            claim = ContinuationRecord(
                receipt=ContinuationReceipt(request=restored_intent, state=ContinuationState.CLAIMED),
                claim_token=uuid4(),
            )
            receipt = ContinuationResumptionReceipt(
                authorization_id=authorization_id, request_id=intent.request_id,
                task_id=task_id, trace_id=intent.trace_id, target_role=intent.target_role,
                resumed_state=restored_task.state, task_revision=restored_intent.expected_revision,
                runtime_revision=restored_intent.runtime_revision,
                authorization_sha256=human_message_digest(grant.model_dump(mode="json")),
                runtime_sha256=human_message_digest(restored_context.model_dump(mode="json")),
            )
            cursor = connection.execute("""UPDATE tasks SET state=?,task_json=?,revision=revision+1,updated_at=?
                WHERE task_id=? AND revision=? AND state='needs_human'""", (
                restored_task.state.value, restored_task.model_dump_json(), restored_task.updated_at.isoformat(),
                str(task_id), intent.expected_revision,
            ))
            if cursor.rowcount != 1:
                raise ContinuationConflictError("task changed during recovery")
            cursor = connection.execute("""UPDATE workflow_runtime_contexts SET context_json=?,revision=revision+1,updated_at=?
                WHERE task_id=? AND revision=?""", (
                restored_context.model_dump_json(), restored_context.updated_at.isoformat(), str(task_id), intent.runtime_revision,
            ))
            if cursor.rowcount != 1:
                raise ContinuationConflictError("runtime changed during recovery")
            try:
                connection.execute("INSERT INTO continuation_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self.claims._columns(claim))
                connection.execute("INSERT INTO continuation_resumptions VALUES (?, ?, ?, ?)", (
                    str(authorization_id), str(intent.request_id), str(task_id), receipt.model_dump_json(),
                ))
            except sqlite3.IntegrityError as exc:
                raise ContinuationConflictError("continuation intent or task already reserved") from exc
            pending = ContinuationRecord(receipt=ContinuationReceipt(request=restored_intent, state=ContinuationState.PENDING))
            self.claims._trace(connection, pending, TraceEventType.CONTINUATION_REQUESTED)
            self.claims._trace(connection, claim, TraceEventType.CONTINUATION_CLAIMED)
            self.claims.traces.append_in_transaction(connection, TraceEvent(
                task_id=task_id, trace_id=intent.trace_id, type=TraceEventType.CONTINUATION_RESUMED,
                actor_kind=TraceActorKind.DETERMINISTIC, actor_id="controlled_resumption",
                correlation_id=intent.correlation_id, causation_id=intent.message_id,
                idempotency_key=f"continuation-resumed:{authorization_id}",
                payload={"authorization_id": str(authorization_id), "request_id": str(intent.request_id),
                         "from_state": "needs_human", "resumed_state": restored_task.state.value,
                         "task_revision": receipt.task_revision, "runtime_revision": receipt.runtime_revision,
                         "historical_evidence_invalidated": True, "agent_dispatched": False, "budget_reset": False},
            ))
            return ClaimedResumption(receipt, claim, TaskSnapshot(task=restored_task, revision=receipt.task_revision), restored_context)

    def _grant(self, connection, task_id, authorization_id):
        row = connection.execute("SELECT * FROM continuation_authorizations WHERE authorization_id=? AND task_id=?",
                                 (str(authorization_id), str(task_id))).fetchone()
        if row is None:
            raise ContinuationNotFoundError("authorization not found for this task")
        return self.authorizations._bound(connection, row)

    def _bound(self, connection, grant, row):
        try:
            receipt = ContinuationResumptionReceipt.model_validate_json(row["receipt_json"])
            intent = grant.intent
            if (str(receipt.authorization_id), str(receipt.request_id), str(receipt.task_id), receipt.model_dump_json()) != (
                row["authorization_id"], row["request_id"], row["task_id"], row["receipt_json"],
            ) or (receipt.authorization_id != grant.authorization_id or receipt.request_id != intent.request_id
                  or receipt.task_id != intent.task_id or receipt.trace_id != intent.trace_id
                  or receipt.target_role != intent.target_role
                  or receipt.task_revision != intent.expected_revision + 1
                  or receipt.runtime_revision != intent.runtime_revision + 1
                  or receipt.authorization_sha256 != human_message_digest(grant.model_dump(mode="json"))):
                raise ValueError("resumption receipt disagrees with grant or index")
            record = self.claims._get_scoped(connection, intent.task_id, intent.request_id)
            restored_intent = ContinuationIntent.model_validate_json(intent.model_copy(update={
                "expected_revision": receipt.task_revision, "runtime_revision": receipt.runtime_revision,
            }).model_dump_json())
            if record.receipt.request != restored_intent:
                raise ValueError("resumption intent disagrees with new claim")
            return receipt
        except (ValueError, TypeError, ContinuationConflictError) as exc:
            raise ContinuationIntegrityError("persisted resumption is corrupt") from exc
