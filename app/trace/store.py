import hashlib
import json
import sqlite3
from uuid import UUID

from app.storage import Migration, SQLiteDatabase
from app.trace.models import StoredTraceEvent, TraceEvent, TraceEventType


class TraceStoreError(RuntimeError):
    """Base error for the append-only trace log."""


class TraceEventNotFoundError(TraceStoreError):
    pass


class TraceIdempotencyConflictError(TraceStoreError):
    pass


TRACE_STORE_MIGRATIONS = (
    Migration(
        version=9,
        name="create_trace_events",
        statements=(
            """
            CREATE TABLE trace_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                correlation_id TEXT,
                causation_id TEXT,
                idempotency_key TEXT NOT NULL,
                event_json TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                UNIQUE(trace_id, idempotency_key)
            )
            """,
            "CREATE INDEX trace_events_task_sequence_idx ON trace_events(task_id, sequence)",
            "CREATE INDEX trace_events_trace_sequence_idx ON trace_events(trace_id, sequence)",
            "CREATE INDEX trace_events_type_idx ON trace_events(event_type, sequence)",
        ),
    ),
)


class TraceStore:
    """Append-only, cursor-readable execution history with idempotent writes."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(TRACE_STORE_MIGRATIONS)

    def append(self, event: TraceEvent) -> StoredTraceEvent:
        with self.database.transaction() as connection:
            return self.append_in_transaction(connection, event)

    def append_in_transaction(
        self, connection: sqlite3.Connection, event: TraceEvent,
    ) -> StoredTraceEvent:
        """Append without committing a caller-owned write transaction.

        The caller must supply this database's active write connection.
        """
        if not connection.in_transaction:
            raise ValueError("trace append requires an active transaction")
        fingerprint = _fingerprint(event)
        existing = connection.execute(
            """SELECT event_id, event_fingerprint FROM trace_events
            WHERE trace_id = ? AND idempotency_key = ?""",
            (str(event.trace_id), event.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["event_fingerprint"] != fingerprint:
                raise TraceIdempotencyConflictError(
                    "trace idempotency key was used for different event content"
                )
            return self._get(connection, UUID(existing["event_id"]))
        connection.execute(
            """INSERT INTO trace_events(
                event_id, task_id, trace_id, event_type, actor_kind, actor_id,
                correlation_id, causation_id, idempotency_key, event_json,
                event_fingerprint, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(event.event_id), str(event.task_id), str(event.trace_id), event.type.value,
                event.actor_kind.value, event.actor_id,
                str(event.correlation_id) if event.correlation_id else None,
                str(event.causation_id) if event.causation_id else None,
                event.idempotency_key, event.model_dump_json(), fingerprint, event.occurred_at.isoformat(),
            ),
        )
        return self._get(connection, event.event_id)

    def get(self, event_id: UUID) -> StoredTraceEvent:
        with self.database.connect() as connection:
            return self._get(connection, event_id)

    def list(
        self,
        *,
        trace_id: UUID | None = None,
        task_id: UUID | None = None,
        type: TraceEventType | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[StoredTraceEvent, ...]:
        if trace_id is None and task_id is None:
            raise ValueError("trace_id or task_id is required")
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = ["sequence > ?"]
        parameters: list[object] = [after_sequence]
        if trace_id is not None:
            clauses.append("trace_id = ?")
            parameters.append(str(trace_id))
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(str(task_id))
        if type is not None:
            clauses.append("event_type = ?")
            parameters.append(type.value)
        parameters.append(limit)
        query = (
            "SELECT event_id FROM trace_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?"
        )
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return tuple(self._get(connection, UUID(row["event_id"])) for row in rows)

    @staticmethod
    def _get(connection: sqlite3.Connection, event_id: UUID) -> StoredTraceEvent:
        row = connection.execute(
            "SELECT * FROM trace_events WHERE event_id = ?", (str(event_id),)
        ).fetchone()
        if row is None:
            raise TraceEventNotFoundError(f"trace event not found: {event_id}")
        return StoredTraceEvent(
            sequence=row["sequence"],
            event=TraceEvent.model_validate_json(row["event_json"]),
        )


def _fingerprint(event: TraceEvent) -> str:
    content = event.model_dump(mode="json", exclude={"event_id"})
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
