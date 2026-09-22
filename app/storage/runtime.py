import sqlite3
from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.agents import AgentRole
from app.orchestration.models import utc_now
from app.storage.sqlite import Migration, SQLiteDatabase
from app.verification import VerificationPlan
from app.workspace import WorktreeHandle


class RuntimeContextRepositoryError(RuntimeError):
    """Base error for persisted workflow runtime context."""


class RuntimeContextNotFoundError(RuntimeContextRepositoryError):
    pass


class RuntimeContextConflictError(RuntimeContextRepositoryError):
    pass


class StaleRuntimeContextRevisionError(RuntimeContextConflictError):
    pass


class RuntimeContextIntegrityError(RuntimeContextRepositoryError):
    pass


class AgentRuntimeBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    role: AgentRole
    agent_name: str = Field(min_length=1, max_length=100)
    native_session_id: str | None = Field(default=None, min_length=1, max_length=500)


class WorkflowRuntimeContext(BaseModel):
    """Durable inputs needed to reconstruct an in-process WorkflowRuntime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    room_id: UUID
    worktree: WorktreeHandle
    verification_plan: VerificationPlan
    agent_bindings: tuple[AgentRuntimeBinding, ...] = Field(min_length=1)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_context(self) -> "WorkflowRuntimeContext":
        if self.worktree.task_id != self.task_id:
            raise ValueError("worktree belongs to another task")
        roles = [binding.role for binding in self.agent_bindings]
        if len(roles) != len(set(roles)):
            raise ValueError("agent binding roles must be unique")
        object.__setattr__(
            self,
            "agent_bindings",
            tuple(sorted(self.agent_bindings, key=lambda binding: binding.role.value)),
        )
        return self


class RuntimeContextSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context: WorkflowRuntimeContext
    revision: int = Field(ge=1)


RUNTIME_CONTEXT_MIGRATIONS = (
    Migration(
        version=8,
        name="create_workflow_runtime_contexts",
        statements=(
            """
            CREATE TABLE workflow_runtime_contexts (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                trace_id TEXT NOT NULL,
                room_id TEXT NOT NULL UNIQUE,
                context_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX workflow_runtime_context_trace_idx
            ON workflow_runtime_contexts(trace_id)
            """,
        ),
    ),
)


class RuntimeContextRepository:
    """SQLite runtime inputs with optimistic concurrency protection."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(RUNTIME_CONTEXT_MIGRATIONS)

    def create(self, context: WorkflowRuntimeContext) -> RuntimeContextSnapshot:
        detached = context.model_copy(deep=True)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM workflow_runtime_contexts WHERE task_id = ?",
                (str(context.task_id),),
            ).fetchone()
            if existing is not None:
                snapshot = self._snapshot_from_row(existing)
                if snapshot.context == detached:
                    return snapshot
                raise RuntimeContextConflictError(
                    "task already has a different runtime context"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_runtime_contexts(
                        task_id, trace_id, room_id, context_json, revision, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        str(detached.task_id),
                        str(detached.trace_id),
                        str(detached.room_id),
                        detached.model_dump_json(),
                        detached.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeContextConflictError(
                    "runtime context conflicts with persisted task or room identity"
                ) from exc
        return RuntimeContextSnapshot(context=detached, revision=1)

    def get(self, task_id: UUID) -> RuntimeContextSnapshot:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runtime_contexts WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        if row is None:
            raise RuntimeContextNotFoundError(
                f"workflow runtime context not found: {task_id}"
            )
        return self._snapshot_from_row(row)

    def save(
        self,
        context: WorkflowRuntimeContext,
        *,
        expected_revision: int,
    ) -> RuntimeContextSnapshot:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        detached = context.model_copy(deep=True)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runtime_contexts WHERE task_id = ?",
                (str(context.task_id),),
            ).fetchone()
            if row is None:
                raise RuntimeContextNotFoundError(
                    f"workflow runtime context not found: {context.task_id}"
                )
            persisted = self._snapshot_from_row(row)
            if persisted.revision != expected_revision:
                raise StaleRuntimeContextRevisionError(
                    f"expected runtime context revision {expected_revision}, "
                    f"found {persisted.revision}"
                )
            self._validate_immutable_fields(persisted.context, detached)
            detached = detached.model_copy(update={"updated_at": utc_now()})
            next_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE workflow_runtime_contexts SET
                    context_json = ?, revision = ?, updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (
                    detached.model_dump_json(),
                    next_revision,
                    detached.updated_at.isoformat(),
                    str(detached.task_id),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRuntimeContextRevisionError(
                    "runtime context revision changed during save"
                )
        return RuntimeContextSnapshot(context=detached, revision=next_revision)

    @staticmethod
    def _validate_immutable_fields(
        persisted: WorkflowRuntimeContext,
        candidate: WorkflowRuntimeContext,
    ) -> None:
        immutable = ("task_id", "trace_id", "room_id", "worktree")
        changed = [
            name
            for name in immutable
            if getattr(persisted, name) != getattr(candidate, name)
        ]
        if changed:
            raise RuntimeContextConflictError(
                f"immutable runtime context fields changed: {', '.join(changed)}"
            )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> RuntimeContextSnapshot:
        try:
            context = WorkflowRuntimeContext.model_validate_json(row["context_json"])
            if (
                str(context.task_id) != row["task_id"]
                or str(context.trace_id) != row["trace_id"]
                or str(context.room_id) != row["room_id"]
                or context.updated_at != datetime.fromisoformat(row["updated_at"])
            ):
                raise ValueError("indexed runtime columns do not match context_json")
            return RuntimeContextSnapshot(context=context, revision=row["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeContextIntegrityError(
                f"persisted runtime context is corrupt: {row['task_id']}"
            ) from exc
