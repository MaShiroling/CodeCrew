"""SQLite persistence for standalone chat, isolated from coding tasks."""

import hashlib
import json
import sqlite3
from uuid import UUID

from app.chat.models import (
    ChatTurnStatus,
    StandaloneChatMessage,
    StandaloneChatRoom,
    StandaloneChatTurn,
    StoredStandaloneChatMessage,
)
from app.orchestration.models import utc_now
from app.storage import Migration, SQLiteDatabase
from app.team.models import MessageDelivery, MessageDeliveryStatus, RoomMember, RoomStatus


class StandaloneChatStoreError(RuntimeError):
    """Base error for standalone chat persistence."""


class StandaloneChatRoomNotFoundError(StandaloneChatStoreError):
    pass


class StandaloneChatMessageNotFoundError(StandaloneChatStoreError):
    pass


class StandaloneChatMemberNotFoundError(StandaloneChatStoreError):
    pass


class StandaloneChatConflictError(StandaloneChatStoreError):
    pass


class StandaloneChatIdempotencyError(StandaloneChatConflictError):
    pass


STANDALONE_CHAT_MIGRATIONS = (
    Migration(
        version=15,
        name="create_standalone_chat",
        statements=(
            """
            CREATE TABLE standalone_chat_rooms (
                room_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """,
            """
            CREATE TABLE standalone_chat_members (
                member_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                UNIQUE(room_id, position),
                UNIQUE(room_id, role),
                UNIQUE(room_id, name)
            )
            """,
            """
            CREATE TABLE standalone_chat_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
                trace_id TEXT NOT NULL,
                sender_id TEXT NOT NULL REFERENCES standalone_chat_members(member_id),
                message_json TEXT NOT NULL,
                message_fingerprint TEXT NOT NULL,
                reply_to TEXT REFERENCES standalone_chat_messages(message_id),
                correlation_id TEXT NOT NULL,
                causation_id TEXT REFERENCES standalone_chat_messages(message_id),
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(room_id, sender_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE standalone_chat_deliveries (
                message_id TEXT NOT NULL REFERENCES standalone_chat_messages(message_id),
                recipient_id TEXT NOT NULL REFERENCES standalone_chat_members(member_id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'acknowledged')),
                acknowledged_at TEXT,
                PRIMARY KEY(message_id, recipient_id)
            )
            """,
            ("CREATE INDEX standalone_chat_room_created_idx "
             "ON standalone_chat_rooms(created_at DESC, room_id)"),
            ("CREATE INDEX standalone_chat_message_room_idx "
             "ON standalone_chat_messages(room_id, sequence)"),
            ("CREATE INDEX standalone_chat_delivery_pending_idx "
             "ON standalone_chat_deliveries(recipient_id, status, message_id)"),
        ),
    ),
    Migration(
        version=16,
        name="create_standalone_chat_turns",
        statements=(
            """
            CREATE TABLE standalone_chat_turns (
                turn_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
                message_id TEXT NOT NULL REFERENCES standalone_chat_messages(message_id),
                recipient_id TEXT NOT NULL REFERENCES standalone_chat_members(member_id),
                correlation_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'running', 'succeeded', 'failed', 'cancelled',
                    'interrupted', 'budget_exhausted'
                )),
                session_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(message_id, recipient_id)
            )
            """,
            "CREATE INDEX standalone_chat_turn_room_idx ON standalone_chat_turns(room_id, created_at)",
            "CREATE INDEX standalone_chat_turn_correlation_idx ON standalone_chat_turns(correlation_id)",
        ),
    ),
    Migration(
        version=18,
        name="create_standalone_chat_discussion_runs",
        statements=(
            """
            CREATE TABLE standalone_chat_discussion_runs (
                run_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
                root_message_id TEXT NOT NULL UNIQUE
                    REFERENCES standalone_chat_messages(message_id),
                correlation_id TEXT NOT NULL,
                run_json TEXT NOT NULL,
                pending_json TEXT NOT NULL,
                active_turn_id TEXT REFERENCES standalone_chat_turns(turn_id)
            )
            """,
            ("CREATE INDEX standalone_chat_discussion_room_idx "
             "ON standalone_chat_discussion_runs(room_id, root_message_id)"),
        ),
    ),
)


class StandaloneChatStore:
    """Durable rooms, directed messages and ACKs; never dispatches an Agent."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(STANDALONE_CHAT_MIGRATIONS)

    def create_room(self, room: StandaloneChatRoom) -> StandaloneChatRoom:
        # model_copy(update=...) bypasses Pydantic validation; validate at the storage edge.
        room = StandaloneChatRoom.model_validate(room.model_dump())
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT room_id FROM standalone_chat_rooms WHERE room_id = ? OR trace_id = ?",
                (str(room.room_id), str(room.trace_id)),
            ).fetchone()
            if existing is not None:
                persisted = self._get_room(connection, UUID(existing["room_id"]))
                if persisted == room:
                    return persisted
                raise StandaloneChatConflictError("chat room ID or trace ID is already in use")
            try:
                connection.execute(
                    """INSERT INTO standalone_chat_rooms(
                        room_id, trace_id, title, status, created_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(room.room_id), str(room.trace_id), room.title, room.status.value,
                        room.created_at.isoformat(),
                        room.closed_at.isoformat() if room.closed_at else None,
                    ),
                )
                connection.executemany(
                    """INSERT INTO standalone_chat_members(
                        member_id, room_id, position, name, role, kind, joined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            str(member.member_id), str(room.room_id), position, member.name,
                            member.role.value, member.kind.value, member.joined_at.isoformat(),
                        )
                        for position, member in enumerate(room.members)
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StandaloneChatConflictError("chat room could not be created") from exc
        return room

    def get_room(self, room_id: UUID) -> StandaloneChatRoom:
        with self.database.connect() as connection:
            return self._get_room(connection, room_id)

    def list_rooms(self, *, limit: int = 50, offset: int = 0) -> tuple[StandaloneChatRoom, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid chat room page")
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT room_id FROM standalone_chat_rooms
                ORDER BY created_at DESC, room_id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            return tuple(self._get_room(connection, UUID(row["room_id"])) for row in rows)

    def close_room(self, room_id: UUID) -> StandaloneChatRoom:
        with self.database.transaction() as connection:
            room = self._get_room(connection, room_id)
            if room.status is RoomStatus.ACTIVE:
                connection.execute(
                    "UPDATE standalone_chat_rooms SET status = ?, closed_at = ? WHERE room_id = ?",
                    (RoomStatus.CLOSED.value, utc_now().isoformat(), str(room_id)),
                )
            return self._get_room(connection, room_id)

    def append_message(self, message: StandaloneChatMessage) -> StoredStandaloneChatMessage:
        message = StandaloneChatMessage.model_validate(message.model_dump())
        fingerprint = _fingerprint(message)
        with self.database.transaction() as connection:
            room = self._get_room(connection, message.room_id)
            if room.trace_id != message.trace_id:
                raise StandaloneChatConflictError("message trace does not match its room")
            members = {member.member_id for member in room.members}
            if message.sender_id not in members or any(
                recipient not in members for recipient in message.recipient_ids
            ):
                raise StandaloneChatMemberNotFoundError("chat sender or recipient is not in room")
            existing = connection.execute(
                """SELECT message_id, message_fingerprint FROM standalone_chat_messages
                WHERE room_id = ? AND sender_id = ? AND idempotency_key = ?""",
                (str(message.room_id), str(message.sender_id), message.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["message_fingerprint"] != fingerprint:
                    raise StandaloneChatIdempotencyError(
                        "idempotency key was used for different chat content"
                    )
                return self._get_message(connection, UUID(existing["message_id"]))
            if room.status is RoomStatus.CLOSED:
                raise StandaloneChatConflictError("cannot append a message to a closed chat")
            if message.context_anchor_id is not None:
                anchor = connection.execute(
                    """SELECT m.room_id, member.role FROM standalone_chat_messages AS m
                    JOIN standalone_chat_members AS member ON member.member_id = m.sender_id
                    WHERE m.message_id = ?""",
                    (str(message.context_anchor_id),),
                ).fetchone()
                if anchor is None or anchor["room_id"] != str(message.room_id):
                    raise StandaloneChatMessageNotFoundError(
                        "context anchor is not in this room"
                    )
                if anchor["role"] != "human":
                    raise StandaloneChatConflictError("context anchor must be a Human message")
            for reference_id in (message.reply_to, message.causation_id):
                if reference_id is None:
                    continue
                parent = connection.execute(
                    """SELECT room_id, correlation_id FROM standalone_chat_messages
                    WHERE message_id = ?""",
                    (str(reference_id),),
                ).fetchone()
                if parent is None or parent["room_id"] != str(message.room_id):
                    raise StandaloneChatMessageNotFoundError(
                        "chat reply or cause is not in this room"
                    )
                if parent["correlation_id"] != str(message.correlation_id):
                    raise StandaloneChatConflictError("chat reply changed the discussion thread")
            try:
                connection.execute(
                    """INSERT INTO standalone_chat_messages(
                        message_id, room_id, trace_id, sender_id, message_json,
                        message_fingerprint, reply_to, correlation_id, causation_id,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(message.message_id), str(message.room_id), str(message.trace_id),
                        str(message.sender_id), message.model_dump_json(), fingerprint,
                        str(message.reply_to) if message.reply_to else None,
                        str(message.correlation_id),
                        str(message.causation_id) if message.causation_id else None,
                        message.idempotency_key, message.created_at.isoformat(),
                    ),
                )
                connection.executemany(
                    """INSERT INTO standalone_chat_deliveries(message_id, recipient_id, status)
                    VALUES (?, ?, ?)""",
                    (
                        (
                            str(message.message_id), str(recipient_id),
                            MessageDeliveryStatus.PENDING.value,
                        )
                        for recipient_id in message.recipient_ids
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StandaloneChatConflictError("chat message could not be appended") from exc
            return self._get_message(connection, message.message_id)

    def get_message(self, message_id: UUID) -> StoredStandaloneChatMessage:
        with self.database.connect() as connection:
            return self._get_message(connection, message_id)

    def list_messages(
        self, room_id: UUID, *, after_sequence: int = 0, limit: int = 100,
    ) -> tuple[StoredStandaloneChatMessage, ...]:
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid chat message page")
        with self.database.connect() as connection:
            self._get_room(connection, room_id)
            rows = connection.execute(
                """SELECT message_id FROM standalone_chat_messages
                WHERE room_id = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
                (str(room_id), after_sequence, limit),
            ).fetchall()
            return tuple(self._get_message(connection, UUID(row["message_id"])) for row in rows)

    def recent_messages_before(
        self, room_id: UUID, *, before_sequence: int,
        correlation_id: UUID | None = None, limit: int = 6,
    ) -> tuple[StoredStandaloneChatMessage, ...]:
        """Return a bounded, chronological context window before one message."""
        if before_sequence < 1 or not 1 <= limit <= 20:
            raise ValueError("invalid chat context window")
        with self.database.connect() as connection:
            self._get_room(connection, room_id)
            rows = connection.execute(
                """SELECT message_id FROM standalone_chat_messages
                WHERE room_id = ? AND sequence < ?
                AND (? IS NULL OR correlation_id = ?)
                ORDER BY sequence DESC LIMIT ?""",
                (
                    str(room_id), before_sequence,
                    str(correlation_id) if correlation_id else None,
                    str(correlation_id) if correlation_id else None,
                    limit,
                ),
            ).fetchall()
            return tuple(
                self._get_message(connection, UUID(row["message_id"]))
                for row in reversed(rows)
            )

    def pending_for(
        self, member_id: UUID, *, correlation_id: UUID | None = None, limit: int = 100,
    ) -> tuple[StoredStandaloneChatMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid pending message limit")
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM standalone_chat_members WHERE member_id = ?",
                (str(member_id),),
            ).fetchone() is None:
                raise StandaloneChatMemberNotFoundError("chat member not found")
            rows = connection.execute(
                """SELECT m.message_id FROM standalone_chat_messages AS m
                JOIN standalone_chat_deliveries AS d ON d.message_id = m.message_id
                WHERE d.recipient_id = ? AND d.status = 'pending'
                AND (? IS NULL OR m.correlation_id = ?)
                ORDER BY m.sequence LIMIT ?""",
                (
                    str(member_id),
                    str(correlation_id) if correlation_id else None,
                    str(correlation_id) if correlation_id else None,
                    limit,
                ),
            ).fetchall()
            return tuple(self._get_message(connection, UUID(row["message_id"])) for row in rows)

    def acknowledge(self, message_id: UUID, *, recipient_id: UUID) -> StoredStandaloneChatMessage:
        with self.database.transaction() as connection:
            self._get_message(connection, message_id)
            updated = connection.execute(
                """UPDATE standalone_chat_deliveries
                SET status = 'acknowledged', acknowledged_at = ?
                WHERE message_id = ? AND recipient_id = ? AND status = 'pending'""",
                (utc_now().isoformat(), str(message_id), str(recipient_id)),
            )
            if updated.rowcount == 0 and connection.execute(
                """SELECT 1 FROM standalone_chat_deliveries
                WHERE message_id = ? AND recipient_id = ?""",
                (str(message_id), str(recipient_id)),
            ).fetchone() is None:
                raise StandaloneChatMemberNotFoundError("chat message was not sent to member")
            return self._get_message(connection, message_id)

    def addressed_agents(self, room_id: UUID, correlation_id: UUID) -> frozenset[UUID]:
        """Agents already claimed in this discussion, including terminal claims."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT recipient_id FROM standalone_chat_turns
                WHERE room_id = ? AND correlation_id = ?""",
                (str(room_id), str(correlation_id)),
            ).fetchall()
            return frozenset(UUID(row["recipient_id"]) for row in rows)

    def claim_turn(
        self, message_id: UUID, recipient_id: UUID, *, max_turns: int,
    ) -> tuple[StandaloneChatTurn | None, bool]:
        """Atomically reserve a delivery; suppress repeated Agent handoffs."""
        from uuid import uuid5

        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        with self.database.transaction() as connection:
            message = self._get_message(connection, message_id).message
            if recipient_id not in message.recipient_ids:
                raise StandaloneChatMemberNotFoundError("chat message was not sent to member")
            member = connection.execute(
                "SELECT role FROM standalone_chat_members WHERE member_id = ? AND room_id = ?",
                (str(recipient_id), str(message.room_id)),
            ).fetchone()
            if member is None or member["role"] not in {"planner", "implementer", "reviewer"}:
                raise StandaloneChatConflictError("chat turn recipient must be an Agent")
            row = connection.execute(
                "SELECT * FROM standalone_chat_turns WHERE message_id = ? AND recipient_id = ?",
                (str(message_id), str(recipient_id)),
            ).fetchone()
            if row is not None:
                return self._turn(row), False
            if connection.execute(
                """SELECT 1 FROM standalone_chat_discussion_runs
                WHERE room_id = ? AND correlation_id = ? LIMIT 1""",
                (str(message.room_id), str(message.correlation_id)),
            ).fetchone() is not None:
                # The opt-in sequential scheduler owns this correlation. Never
                # let legacy fanout create a parallel Agent delivery.
                return None, False
            sender = connection.execute(
                "SELECT role FROM standalone_chat_members WHERE member_id = ?",
                (str(message.sender_id),),
            ).fetchone()
            if sender is not None and sender["role"] != "human":
                already_addressed = connection.execute(
                    """SELECT 1 FROM standalone_chat_turns
                    WHERE room_id = ? AND correlation_id = ? AND recipient_id = ? LIMIT 1""",
                    (str(message.room_id), str(message.correlation_id), str(recipient_id)),
                ).fetchone()
                if already_addressed is not None:
                    connection.execute(
                        """UPDATE standalone_chat_deliveries
                        SET status = 'acknowledged', acknowledged_at = ?
                        WHERE message_id = ? AND recipient_id = ? AND status = 'pending'""",
                        (utc_now().isoformat(), str(message_id), str(recipient_id)),
                    )
                    return None, False
            count = connection.execute(
                "SELECT COUNT(*) FROM standalone_chat_turns WHERE correlation_id = ?",
                (str(message.correlation_id),),
            ).fetchone()[0]
            status = (
                ChatTurnStatus.QUEUED if count < max_turns else ChatTurnStatus.BUDGET_EXHAUSTED
            )
            now = utc_now().isoformat()
            turn_id = uuid5(message_id, str(recipient_id))
            connection.execute(
                """INSERT INTO standalone_chat_turns (
                    turn_id, room_id, message_id, recipient_id, correlation_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(turn_id), str(message.room_id), str(message_id), str(recipient_id),
                 str(message.correlation_id), status.value, now, now),
            )
            if status is ChatTurnStatus.BUDGET_EXHAUSTED:
                connection.execute(
                    """UPDATE standalone_chat_deliveries
                    SET status = 'acknowledged', acknowledged_at = ?
                    WHERE message_id = ? AND recipient_id = ?""",
                    (now, str(message_id), str(recipient_id)),
                )
            return self._turn(connection.execute(
                "SELECT * FROM standalone_chat_turns WHERE turn_id = ?", (str(turn_id),),
            ).fetchone()), status is ChatTurnStatus.QUEUED

    def get_turn(self, turn_id: UUID) -> StandaloneChatTurn:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_turns WHERE turn_id = ?", (str(turn_id),),
            ).fetchone()
            if row is None:
                raise StandaloneChatMessageNotFoundError("chat turn not found")
            return self._turn(row)

    def list_turns(self, room_id: UUID) -> tuple[StandaloneChatTurn, ...]:
        with self.database.connect() as connection:
            self._get_room(connection, room_id)
            rows = connection.execute(
                """SELECT * FROM standalone_chat_turns WHERE room_id = ?
                ORDER BY created_at, turn_id""", (str(room_id),),
            ).fetchall()
            return tuple(self._turn(row) for row in rows)

    def transition_turn(
        self, turn_id: UUID, *, from_status: ChatTurnStatus,
        to_status: ChatTurnStatus, session_id: UUID | None = None,
        error: str | None = None,
    ) -> bool:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE standalone_chat_turns SET status = ?, session_id = COALESCE(?, session_id),
                error = ?, updated_at = ? WHERE turn_id = ? AND status = ?""",
                (to_status.value, str(session_id) if session_id else None,
                 error[:500] if error else None, utc_now().isoformat(), str(turn_id),
                 from_status.value),
            )
            return changed.rowcount == 1

    def interrupt_unfinished_turns(self) -> int:
        """Startup fence: never launch queued or possibly-running old work."""
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE standalone_chat_turns SET status = 'interrupted',
                error = 'process stopped before a confirmed turn result', updated_at = ?
                WHERE status IN ('queued', 'running')
                AND NOT EXISTS (
                    SELECT 1 FROM standalone_chat_discussion_runs AS bounded
                    WHERE bounded.correlation_id = standalone_chat_turns.correlation_id
                )""",
                (utc_now().isoformat(),),
            )
            return changed.rowcount

    @staticmethod
    def _turn(row: sqlite3.Row) -> StandaloneChatTurn:
        return StandaloneChatTurn.model_validate(dict(row))

    @staticmethod
    def _get_room(connection: sqlite3.Connection, room_id: UUID) -> StandaloneChatRoom:
        row = connection.execute(
            "SELECT * FROM standalone_chat_rooms WHERE room_id = ?", (str(room_id),),
        ).fetchone()
        if row is None:
            raise StandaloneChatRoomNotFoundError(f"chat room not found: {room_id}")
        members = connection.execute(
            """SELECT * FROM standalone_chat_members
            WHERE room_id = ? ORDER BY position""",
            (str(room_id),),
        ).fetchall()
        return StandaloneChatRoom(
            room_id=row["room_id"], trace_id=row["trace_id"], title=row["title"],
            status=row["status"], created_at=row["created_at"], closed_at=row["closed_at"],
            members=tuple(
                RoomMember.model_validate({
                    key: member[key]
                    for key in ("member_id", "room_id", "name", "role", "kind", "joined_at")
                })
                for member in members
            ),
        )

    @staticmethod
    def _get_message(
        connection: sqlite3.Connection, message_id: UUID,
    ) -> StoredStandaloneChatMessage:
        row = connection.execute(
            "SELECT * FROM standalone_chat_messages WHERE message_id = ?",
            (str(message_id),),
        ).fetchone()
        if row is None:
            raise StandaloneChatMessageNotFoundError(f"chat message not found: {message_id}")
        message = StandaloneChatMessage.model_validate_json(row["message_json"])
        deliveries = connection.execute(
            "SELECT * FROM standalone_chat_deliveries WHERE message_id = ?",
            (str(message_id),),
        ).fetchall()
        by_recipient = {
            UUID(delivery["recipient_id"]): MessageDelivery.model_validate(dict(delivery))
            for delivery in deliveries
        }
        return StoredStandaloneChatMessage(
            sequence=row["sequence"], message=message,
            deliveries=tuple(by_recipient[recipient] for recipient in message.recipient_ids),
        )


def _fingerprint(message: StandaloneChatMessage) -> str:
    excluded = {"message_id", "created_at"}
    if message.context_anchor_id is None:
        excluded.add("context_anchor_id")  # Preserve fingerprints for pre-upgrade messages.
    content = message.model_dump(mode="json", exclude=excluded)
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
