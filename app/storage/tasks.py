import sqlite3
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.orchestration.models import Task, TaskState, utc_now
from app.storage.sqlite import Migration, SQLiteDatabase


class TaskRepositoryError(RuntimeError):
    """Base error for durable Task snapshots."""


class TaskNotFoundError(TaskRepositoryError):
    pass


class TaskConflictError(TaskRepositoryError):
    pass


class StaleTaskRevisionError(TaskConflictError):
    pass


class TaskRepositoryIntegrityError(TaskRepositoryError):
    pass


class TaskSnapshot(BaseModel):
    """A detached Task value paired with its optimistic-lock revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: Task
    revision: int = Field(ge=1)


TASK_REPOSITORY_MIGRATIONS = (
    Migration(
        version=7,
        name="create_tasks",
        statements=(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                rework_rounds INTEGER NOT NULL CHECK(rework_rounds >= 0),
                task_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX tasks_state_updated_idx ON tasks(state, updated_at)",
        ),
    ),
)


class TaskRepository:
    """SQLite Task persistence with detached snapshots and optimistic locking."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(TASK_REPOSITORY_MIGRATIONS)

    def create(self, task: Task) -> TaskSnapshot:
        detached = task.model_copy(deep=True)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(task.id),)
            ).fetchone()
            if existing is not None:
                snapshot = self._snapshot_from_row(existing)
                if snapshot.task == detached:
                    return snapshot
                raise TaskConflictError("task ID already exists with different content")
            try:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, trace_id, state, rework_rounds, task_json,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        str(detached.id),
                        str(detached.trace_id),
                        detached.state.value,
                        detached.rework_rounds,
                        detached.model_dump_json(),
                        detached.created_at.isoformat(),
                        detached.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TaskConflictError(
                    "task identity conflicts with an existing task"
                ) from exc
        return TaskSnapshot(task=detached, revision=1)

    def get(self, task_id: UUID) -> TaskSnapshot:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return self._snapshot_from_row(row)

    def save(self, task: Task, *, expected_revision: int) -> TaskSnapshot:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        detached = task.model_copy(deep=True)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(task.id),)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task not found: {task.id}")
            persisted = self._snapshot_from_row(row)
            if persisted.revision != expected_revision:
                raise StaleTaskRevisionError(
                    f"expected task revision {expected_revision}, "
                    f"found {persisted.revision}"
                )
            self._validate_immutable_fields(persisted.task, detached)
            detached.updated_at = utc_now()
            next_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE tasks SET
                    state = ?, rework_rounds = ?, task_json = ?, revision = ?,
                    updated_at = ?
                WHERE task_id = ? AND revision = ?
                """,
                (
                    detached.state.value,
                    detached.rework_rounds,
                    detached.model_dump_json(),
                    next_revision,
                    detached.updated_at.isoformat(),
                    str(detached.id),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskRevisionError("task revision changed during save")
        return TaskSnapshot(task=detached, revision=next_revision)

    def list(
        self,
        *,
        state: TaskState | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[TaskSnapshot, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        query = "SELECT * FROM tasks"
        parameters: list[object] = []
        if state is not None:
            query += " WHERE state = ?"
            parameters.append(state.value)
        query += " ORDER BY created_at, task_id LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._snapshot_from_row(row) for row in rows)

    @staticmethod
    def _validate_immutable_fields(persisted: Task, candidate: Task) -> None:
        immutable = ("id", "trace_id", "issue", "repository_path", "created_at")
        changed = [
            name
            for name in immutable
            if getattr(persisted, name) != getattr(candidate, name)
        ]
        if changed:
            raise TaskConflictError(
                f"immutable task fields changed: {', '.join(changed)}"
            )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> TaskSnapshot:
        try:
            task = Task.model_validate_json(row["task_json"])
            if (
                str(task.id) != row["task_id"]
                or str(task.trace_id) != row["trace_id"]
                or task.state.value != row["state"]
                or task.rework_rounds != row["rework_rounds"]
                or task.created_at != datetime.fromisoformat(row["created_at"])
                or task.updated_at != datetime.fromisoformat(row["updated_at"])
            ):
                raise ValueError("indexed task columns do not match task_json")
            return TaskSnapshot(task=task, revision=row["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskRepositoryIntegrityError(
                f"persisted task is corrupt: {row['task_id']}"
            ) from exc
