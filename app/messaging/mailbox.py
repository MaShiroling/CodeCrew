import hashlib
import json
import sqlite3
from datetime import datetime
from uuid import UUID

from app.messaging.models import (
    HandoffEnvelope,
    HandoffParty,
    MailboxMessage,
    MailboxMessageStatus,
)
from app.orchestration.models import utc_now
from app.storage.sqlite import Migration, SQLiteDatabase


class MailboxError(RuntimeError):
    """Base error for durable A2A message delivery."""


class MailboxMessageNotFoundError(MailboxError):
    pass


class IdempotencyConflictError(MailboxError):
    pass


class InvalidAcknowledgementError(MailboxError):
    pass


MAILBOX_MIGRATIONS = (
    Migration(
        version=2,
        name="create_mailbox_messages",
        statements=(
            """
            CREATE TABLE mailbox_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                idempotency_key TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                message_type TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'delivered', 'acknowledged', 'failed')
                ),
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                acknowledged_at TEXT,
                failed_at TEXT,
                failure_reason TEXT,
                UNIQUE(task_id, sender, idempotency_key)
            )
            """,
            """
            CREATE INDEX mailbox_recipient_status_idx
            ON mailbox_messages(recipient, status, sequence)
            """,
            """
            CREATE INDEX mailbox_task_idx
            ON mailbox_messages(task_id, sequence)
            """,
            """
            CREATE INDEX mailbox_correlation_idx
            ON mailbox_messages(correlation_id, sequence)
            """,
        ),
    ),
)


class Mailbox:
    """SQLite-backed durable mailbox with explicit acknowledgement."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(MAILBOX_MIGRATIONS)

    def send(self, envelope: HandoffEnvelope) -> MailboxMessage:
        envelope_json = envelope.model_dump_json()
        fingerprint = _fingerprint(envelope)
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM mailbox_messages
                WHERE task_id = ? AND sender = ? AND idempotency_key = ?
                """,
                (str(envelope.task_id), envelope.sender.value, envelope.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["envelope_fingerprint"] != fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency key was already used for different message content"
                    )
                return _message_from_row(existing)

            connection.execute(
                """
                INSERT INTO mailbox_messages(
                    message_id, task_id, trace_id, correlation_id, causation_id,
                    idempotency_key, sender, recipient, message_type,
                    envelope_json, envelope_fingerprint, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(envelope.message_id),
                    str(envelope.task_id),
                    str(envelope.trace_id),
                    str(envelope.correlation_id),
                    str(envelope.causation_id) if envelope.causation_id else None,
                    envelope.idempotency_key,
                    envelope.sender.value,
                    envelope.recipient.value,
                    envelope.type.value,
                    envelope_json,
                    fingerprint,
                    MailboxMessageStatus.PENDING.value,
                    envelope.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM mailbox_messages WHERE message_id = ?",
                (str(envelope.message_id),),
            ).fetchone()
        return _message_from_row(row)

    def receive(
        self,
        recipient: HandoffParty,
        *,
        task_id: UUID | None = None,
        limit: int = 10,
    ) -> tuple[MailboxMessage, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        delivered_at = utc_now().isoformat()
        with self.database.transaction() as connection:
            task_clause = " AND task_id = ?" if task_id is not None else ""
            parameters: tuple[str | int, ...] = (
                recipient.value,
                MailboxMessageStatus.PENDING.value,
                *((str(task_id),) if task_id is not None else ()),
                limit,
            )
            rows = connection.execute(
                f"""
                SELECT sequence FROM mailbox_messages
                WHERE recipient = ? AND status = ?{task_clause}
                ORDER BY sequence LIMIT ?
                """,
                parameters,
            ).fetchall()
            sequences = [row["sequence"] for row in rows]
            if not sequences:
                return ()
            placeholders = ",".join("?" for _ in sequences)
            connection.execute(
                f"""
                UPDATE mailbox_messages
                SET status = ?, delivered_at = ?
                WHERE sequence IN ({placeholders}) AND status = ?
                """,
                (
                    MailboxMessageStatus.DELIVERED.value,
                    delivered_at,
                    *sequences,
                    MailboxMessageStatus.PENDING.value,
                ),
            )
            claimed = connection.execute(
                f"SELECT * FROM mailbox_messages WHERE sequence IN ({placeholders}) ORDER BY sequence",
                sequences,
            ).fetchall()
        return tuple(_message_from_row(row) for row in claimed)

    def acknowledge(self, message_id: UUID, *, recipient: HandoffParty) -> MailboxMessage:
        with self.database.transaction() as connection:
            row = _require_row(connection, message_id)
            if row["recipient"] != recipient.value:
                raise InvalidAcknowledgementError("only the message recipient may acknowledge it")
            status = MailboxMessageStatus(row["status"])
            if status is MailboxMessageStatus.PENDING:
                raise InvalidAcknowledgementError("message must be delivered before acknowledgement")
            if status is MailboxMessageStatus.FAILED:
                raise InvalidAcknowledgementError("failed message cannot be acknowledged")
            if status is MailboxMessageStatus.DELIVERED:
                connection.execute(
                    """
                    UPDATE mailbox_messages SET status = ?, acknowledged_at = ?
                    WHERE message_id = ?
                    """,
                    (
                        MailboxMessageStatus.ACKNOWLEDGED.value,
                        utc_now().isoformat(),
                        str(message_id),
                    ),
                )
            updated = _require_row(connection, message_id)
        return _message_from_row(updated)

    def mark_failed(self, message_id: UUID, *, reason: str) -> MailboxMessage:
        reason = reason.strip()
        if not reason:
            raise ValueError("failure reason must not be empty")
        if len(reason) > 2000:
            raise ValueError("failure reason must not exceed 2000 characters")
        with self.database.transaction() as connection:
            row = _require_row(connection, message_id)
            if row["status"] == MailboxMessageStatus.ACKNOWLEDGED.value:
                raise MailboxError("acknowledged message cannot be marked failed")
            connection.execute(
                """
                UPDATE mailbox_messages
                SET status = ?, failed_at = ?, failure_reason = ?
                WHERE message_id = ?
                """,
                (
                    MailboxMessageStatus.FAILED.value,
                    utc_now().isoformat(),
                    reason,
                    str(message_id),
                ),
            )
            updated = _require_row(connection, message_id)
        return _message_from_row(updated)

    def get(self, message_id: UUID) -> MailboxMessage:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mailbox_messages WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
        if row is None:
            raise MailboxMessageNotFoundError(f"mailbox message not found: {message_id}")
        return _message_from_row(row)

    def list_messages(
        self,
        *,
        task_id: UUID | None = None,
        recipient: HandoffParty | None = None,
        status: MailboxMessageStatus | None = None,
        correlation_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[MailboxMessage, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[str | int] = []
        for column, value in (
            ("task_id", task_id),
            ("recipient", recipient.value if recipient else None),
            ("status", status.value if status else None),
            ("correlation_id", correlation_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(str(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM mailbox_messages{where} ORDER BY sequence LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(_message_from_row(row) for row in rows)


def _fingerprint(envelope: HandoffEnvelope) -> str:
    content = envelope.model_dump(mode="json", exclude={"message_id", "created_at"})
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_row(connection: sqlite3.Connection, message_id: UUID) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM mailbox_messages WHERE message_id = ?", (str(message_id),)
    ).fetchone()
    if row is None:
        raise MailboxMessageNotFoundError(f"mailbox message not found: {message_id}")
    return row


def _message_from_row(row: sqlite3.Row) -> MailboxMessage:
    def timestamp(name: str) -> datetime | None:
        value = row[name]
        return datetime.fromisoformat(value) if value else None

    return MailboxMessage(
        sequence=row["sequence"],
        envelope=HandoffEnvelope.model_validate_json(row["envelope_json"]),
        status=row["status"],
        delivered_at=timestamp("delivered_at"),
        acknowledged_at=timestamp("acknowledged_at"),
        failed_at=timestamp("failed_at"),
        failure_reason=row["failure_reason"],
    )
