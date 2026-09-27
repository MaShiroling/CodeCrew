"""Audited new Human intent after a committed turn; not an execution permit."""

import json
import sqlite3
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentRole
from app.orchestration.models import utc_now
from app.storage import ArtifactReference, Migration
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationIntent,
    ContinuationNotFoundError,
    ContinuationState,
    human_message_digest,
)
from app.trace import TraceActorKind, TraceEvent, TraceEventType


class AuthorizeContinuationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    message_id: UUID
    target_role: Literal[AgentRole.PLANNER, AgentRole.IMPLEMENTER, AgentRole.REVIEWER]
    expected_revision: int = Field(ge=1, strict=True)
    expected_runtime_revision: int = Field(ge=1, strict=True)
    expected_claim_updated_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=1000)


class ContinuationAuthorizationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: UUID = Field(default_factory=uuid4)
    previous_request_id: UUID
    human_member_id: UUID
    command: AuthorizeContinuationCommand
    intent: ContinuationIntent
    previous_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[ArtifactReference, ...] = ()
    execution_ready: Literal[False] = False
    agent_dispatched: Literal[False] = False
    claim_released: Literal[False] = False
    budget_reset: Literal[False] = False
    task_completion_evaluated: Literal[False] = False
    external_process_stopped_confirmed: Literal[False] = False
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_intent(self):
        if (self.intent.idempotency_key != self.command.idempotency_key
                or self.intent.message_id != self.command.message_id
                or self.intent.target_role != self.command.target_role
                or self.intent.expected_revision != self.command.expected_revision
                or self.intent.runtime_revision != self.command.expected_runtime_revision
                or self.intent.request_id == self.previous_request_id):
            raise ValueError("authorization command does not match new intent")
        return self


AUTHORIZATION_MIGRATIONS = (Migration(
    version=13, name="create_continuation_authorizations", statements=(
        """CREATE TABLE continuation_authorizations (
        authorization_id TEXT PRIMARY KEY,
        previous_request_id TEXT NOT NULL REFERENCES continuation_requests(request_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        idempotency_key TEXT NOT NULL,
        message_id TEXT NOT NULL REFERENCES chat_messages(message_id),
        runtime_revision INTEGER NOT NULL, receipt_json TEXT NOT NULL,
        UNIQUE(task_id, idempotency_key), UNIQUE(task_id, message_id),
        UNIQUE(task_id, runtime_revision))""",
    ),
),)


class ContinuationAuthorizationRepository:
    def __init__(self, claims):
        self.claims = claims
        self.database = claims.database

    def initialize(self):
        self.database.initialize(AUTHORIZATION_MIGRATIONS)

    def get(self, *, task_id, authorization_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM continuation_authorizations WHERE task_id=? AND authorization_id=?",
                (str(task_id), str(authorization_id)),
            ).fetchone()
            if row is None:
                raise ContinuationNotFoundError("authorization not found for this task")
            return self._bound(connection, row)

    def replay(self, *, task_id, previous_request_id, command, human_member_id):
        command = AuthorizeContinuationCommand.model_validate_json(command.model_dump_json())
        with self.database.connect() as connection:
            self.claims._get_scoped(connection, task_id, previous_request_id)
            row = connection.execute(
                "SELECT * FROM continuation_authorizations WHERE task_id=? AND idempotency_key=?",
                (str(task_id), str(command.idempotency_key)),
            ).fetchone()
            if row is None:
                return None
            receipt = self._bound(connection, row)
            if (receipt.previous_request_id != previous_request_id or receipt.command != command
                    or receipt.human_member_id != human_member_id):
                raise ContinuationConflictError("authorization key conflicts with stored intent")
            return receipt

    def authorize(self, *, previous_request_id, human_member_id, command, intent,
                  runtime_sha256, artifacts):
        # model_copy does not validate updates: revalidate at the persistence boundary.
        command = AuthorizeContinuationCommand.model_validate_json(command.model_dump_json())
        intent = ContinuationIntent.model_validate_json(intent.model_dump_json())
        with self.database.transaction() as connection:
            previous = self.claims._get_scoped(connection, intent.task_id, previous_request_id)
            row = connection.execute(
                "SELECT * FROM continuation_authorizations WHERE task_id=? AND idempotency_key=?",
                (str(intent.task_id), str(command.idempotency_key)),
            ).fetchone()
            if row is not None:
                receipt = self._bound(connection, row)
                if (receipt.previous_request_id != previous_request_id or receipt.command != command
                        or receipt.human_member_id != human_member_id):
                    raise ContinuationConflictError("authorization key conflicts with stored intent")
                return receipt
            if (previous.receipt.state is not ContinuationState.SUCCEEDED
                    or previous.updated_at != command.expected_claim_updated_at
                    or previous.receipt.runtime_revision != intent.runtime_revision
                    or previous.receipt.request.trace_id != intent.trace_id
                    or previous.receipt.request.room_id != intent.room_id
                    or previous.receipt.request.message_id == intent.message_id
                    or self.claims._quarantine_for(connection, previous_request_id) is not None):
                raise ContinuationConflictError("new intent requires the latest committed turn; unresolved claims cannot be released")
            if connection.execute(
                "SELECT request_id FROM continuation_requests WHERE task_id=? AND state!='succeeded'",
                (str(intent.task_id),),
            ).fetchone() is not None:
                raise ContinuationConflictError("task has an unresolved continuation reservation")
            context = self.claims._validate_current(connection, intent)
            if human_message_digest(context.model_dump(mode="json")) != runtime_sha256:
                raise ContinuationConflictError("runtime changed during authorization")
            source = self.claims._require_message_scope(connection, intent, intent.message_id)
            humans = connection.execute(
                "SELECT * FROM room_members WHERE room_id=? AND role='human'",
                (str(intent.room_id),),
            ).fetchall()
            if (len(humans) != 1 or humans[0]["kind"] != "human"
                    or humans[0]["member_id"] != str(human_member_id)
                    or source["sender_id"] != str(human_member_id)):
                raise ContinuationConflictError("authorization requires the room's unique Human")
            if connection.execute(
                "SELECT request_id FROM continuation_requests WHERE task_id=? AND (message_id=? OR idempotency_key=?)",
                (str(intent.task_id), str(intent.message_id), str(intent.idempotency_key)),
            ).fetchone() is not None:
                raise ContinuationConflictError("new Human intent/key already has an execution reservation")
            receipt = ContinuationAuthorizationReceipt(
                previous_request_id=previous_request_id, human_member_id=human_member_id,
                command=command, intent=intent,
                previous_record_sha256=self.claims._record_digest(previous),
                runtime_sha256=runtime_sha256, artifacts=artifacts,
            )
            for reference in receipt.artifacts:
                artifact = connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (str(reference.artifact_id),)).fetchone()
                if artifact is None or (artifact["task_id"], artifact["trace_id"], artifact["sha256"], artifact["artifact_type"]) != (
                    str(intent.task_id), str(intent.trace_id), reference.sha256, reference.type.value,
                ):
                    raise ContinuationConflictError("authorization Artifact scope changed")
            try:
                connection.execute("INSERT INTO continuation_authorizations VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    str(receipt.authorization_id), str(previous_request_id), str(intent.task_id),
                    str(command.idempotency_key), str(intent.message_id), intent.runtime_revision,
                    receipt.model_dump_json(),
                ))
            except sqlite3.IntegrityError as exc:
                raise ContinuationConflictError("this message/runtime already has an authorization") from exc
            self.claims.traces.append_in_transaction(connection, TraceEvent(
                task_id=intent.task_id, trace_id=intent.trace_id,
                type=TraceEventType.CONTINUATION_AUTHORIZED,
                actor_kind=TraceActorKind.HUMAN, actor_id=str(human_member_id),
                correlation_id=intent.correlation_id, causation_id=intent.message_id,
                idempotency_key=f"continuation-authorization:{receipt.authorization_id}",
                payload={"authorization_id": str(receipt.authorization_id),
                         "previous_request_id": str(previous_request_id), "message_id": str(intent.message_id),
                         "target_role": intent.target_role.value, "reason": command.reason,
                         "execution_ready": False, "claim_released": False, "budget_reset": False},
            ))
            return receipt

    def _bound(self, connection, row):
        try:
            receipt = ContinuationAuthorizationReceipt.model_validate_json(row["receipt_json"])
            if (str(receipt.authorization_id), str(receipt.previous_request_id), str(receipt.intent.task_id),
                    str(receipt.command.idempotency_key), str(receipt.intent.message_id), receipt.intent.runtime_revision,
                    receipt.model_dump_json()) != tuple(row[key] for key in (
                        "authorization_id", "previous_request_id", "task_id", "idempotency_key", "message_id", "runtime_revision", "receipt_json")):
                raise ValueError("authorization index disagrees with receipt")
            previous = self.claims._get_scoped(connection, receipt.intent.task_id, receipt.previous_request_id)
            if (previous.receipt.state is not ContinuationState.SUCCEEDED
                    or self.claims._record_digest(previous) != receipt.previous_record_sha256
                    or previous.receipt.request.trace_id != receipt.intent.trace_id
                    or previous.receipt.request.room_id != receipt.intent.room_id
                    or previous.updated_at != receipt.command.expected_claim_updated_at
                    or previous.receipt.runtime_revision != receipt.intent.runtime_revision
                    or previous.receipt.request.message_id == receipt.intent.message_id):
                raise ValueError("authorization predecessor binding changed")
            source = self.claims._require_message_scope(connection, receipt.intent, receipt.intent.message_id)
            if (source["sender_id"] != str(receipt.human_member_id)
                    or human_message_digest(json.loads(source["message_json"])) != receipt.intent.source_sha256
                    or source["correlation_id"] != str(receipt.intent.correlation_id)):
                raise ValueError("authorization Human intent binding changed")
            return receipt
        except (ValueError, TypeError, ContinuationConflictError) as exc:
            raise ContinuationIntegrityError("persisted authorization is corrupt") from exc
