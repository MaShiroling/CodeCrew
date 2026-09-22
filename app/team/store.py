import hashlib
import json
import sqlite3
from datetime import datetime
from uuid import UUID

from app.orchestration.models import utc_now
from app.storage import Migration, SQLiteDatabase
from app.team.models import (
    ChatMessage,
    MessageDelivery,
    MessageDeliveryStatus,
    RoomMember,
    RoomStatus,
    StoredChatMessage,
    TeamRoom,
)


class TeamRoomStoreError(RuntimeError):
    """Base error for room and chat persistence."""


class RoomNotFoundError(TeamRoomStoreError):
    pass


class MemberNotFoundError(TeamRoomStoreError):
    pass


class ChatMessageNotFoundError(TeamRoomStoreError):
    pass


class RoomConflictError(TeamRoomStoreError):
    pass


class ChatIdempotencyConflictError(TeamRoomStoreError):
    pass


class InvalidChatAcknowledgementError(TeamRoomStoreError):
    pass


TEAM_ROOM_MIGRATIONS = (
    Migration(
        version=3,
        name="create_team_rooms_and_chat_messages",
        statements=(
            """
            CREATE TABLE team_rooms (
                room_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                trace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """,
            """
            CREATE TABLE room_members (
                member_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES team_rooms(room_id),
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                UNIQUE(room_id, name)
            )
            """,
            """
            CREATE TABLE chat_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                room_id TEXT NOT NULL REFERENCES team_rooms(room_id),
                task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                sender_id TEXT NOT NULL REFERENCES room_members(member_id),
                message_type TEXT NOT NULL,
                message_json TEXT NOT NULL,
                message_fingerprint TEXT NOT NULL,
                reply_to TEXT,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(room_id, sender_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE chat_deliveries (
                message_id TEXT NOT NULL REFERENCES chat_messages(message_id),
                recipient_id TEXT NOT NULL REFERENCES room_members(member_id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'acknowledged')),
                acknowledged_at TEXT,
                PRIMARY KEY(message_id, recipient_id)
            )
            """,
            "CREATE INDEX chat_room_sequence_idx ON chat_messages(room_id, sequence)",
            "CREATE INDEX chat_reply_idx ON chat_messages(reply_to, sequence)",
            """
            CREATE INDEX chat_delivery_recipient_idx
            ON chat_deliveries(recipient_id, status, message_id)
            """,
        ),
    ),
)


class TeamRoomStore:
    """SQLite event log for task rooms with per-recipient acknowledgement."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize(TEAM_ROOM_MIGRATIONS)

    def create_room(self, room: TeamRoom) -> TeamRoom:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT room_id FROM team_rooms WHERE task_id = ?",
                (str(room.task_id),),
            ).fetchone()
            if existing is not None:
                persisted = self._get_room(connection, UUID(existing["room_id"]))
                if persisted == room:
                    return persisted
                raise RoomConflictError("task already has a different team room")
            connection.execute(
                """
                INSERT INTO team_rooms(
                    room_id, task_id, trace_id, name, status, created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(room.room_id),
                    str(room.task_id),
                    str(room.trace_id),
                    room.name,
                    room.status.value,
                    room.created_at.isoformat(),
                    room.closed_at.isoformat() if room.closed_at else None,
                ),
            )
            for member in room.members:
                self._insert_member(connection, member)
        return room

    def add_member(self, member: RoomMember) -> RoomMember:
        with self.database.transaction() as connection:
            room = self._get_room(connection, member.room_id)
            if room.status is RoomStatus.CLOSED:
                raise RoomConflictError("cannot add a member to a closed room")
            try:
                self._insert_member(connection, member)
            except sqlite3.IntegrityError as exc:
                raise RoomConflictError("room member already exists") from exc
        return member

    def get_room(self, room_id: UUID) -> TeamRoom:
        with self.database.connect() as connection:
            return self._get_room(connection, room_id)

    def get_member(self, member_id: UUID) -> RoomMember:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM room_members WHERE member_id = ?", (str(member_id),)
            ).fetchone()
        if row is None:
            raise MemberNotFoundError(f"room member not found: {member_id}")
        return _member_from_row(row)

    def close_room(self, room_id: UUID) -> TeamRoom:
        with self.database.transaction() as connection:
            room = self._get_room(connection, room_id)
            if room.status is RoomStatus.ACTIVE:
                connection.execute(
                    "UPDATE team_rooms SET status = ?, closed_at = ? WHERE room_id = ?",
                    (RoomStatus.CLOSED.value, utc_now().isoformat(), str(room_id)),
                )
            return self._get_room(connection, room_id)

    def append_message(
        self,
        message: ChatMessage,
        *,
        recipient_ids: tuple[UUID, ...],
    ) -> StoredChatMessage:
        if not recipient_ids:
            raise ValueError("at least one resolved recipient is required")
        if len(recipient_ids) != len(set(recipient_ids)):
            raise ValueError("resolved recipient IDs must be unique")
        fingerprint = _fingerprint(message, recipient_ids)
        with self.database.transaction() as connection:
            room = self._get_room(connection, message.room_id)
            if room.status is RoomStatus.CLOSED:
                raise RoomConflictError("cannot append a message to a closed room")
            if room.task_id != message.task_id or room.trace_id != message.trace_id:
                raise RoomConflictError("message task or trace does not match its room")
            members = {member.member_id for member in room.members}
            if message.sender_id not in members:
                raise MemberNotFoundError("message sender is not a room member")
            if any(recipient not in members for recipient in recipient_ids):
                raise MemberNotFoundError("message recipient is not a room member")
            if message.reply_to is not None:
                reply = connection.execute(
                    "SELECT room_id FROM chat_messages WHERE message_id = ?",
                    (str(message.reply_to),),
                ).fetchone()
                if reply is None or reply["room_id"] != str(message.room_id):
                    raise ChatMessageNotFoundError("reply target is not in this room")

            existing = connection.execute(
                """
                SELECT message_id, message_fingerprint FROM chat_messages
                WHERE room_id = ? AND sender_id = ? AND idempotency_key = ?
                """,
                (str(message.room_id), str(message.sender_id), message.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["message_fingerprint"] != fingerprint:
                    raise ChatIdempotencyConflictError(
                        "idempotency key was used for different chat content"
                    )
                return self._get_message(connection, UUID(existing["message_id"]))

            connection.execute(
                """
                INSERT INTO chat_messages(
                    message_id, room_id, task_id, trace_id, sender_id, message_type,
                    message_json, message_fingerprint, reply_to, correlation_id,
                    causation_id, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(message.message_id),
                    str(message.room_id),
                    str(message.task_id),
                    str(message.trace_id),
                    str(message.sender_id),
                    message.type.value,
                    message.model_dump_json(),
                    fingerprint,
                    str(message.reply_to) if message.reply_to else None,
                    str(message.correlation_id),
                    str(message.causation_id) if message.causation_id else None,
                    message.idempotency_key,
                    message.created_at.isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO chat_deliveries(message_id, recipient_id, status)
                VALUES (?, ?, ?)
                """,
                (
                    (
                        str(message.message_id),
                        str(recipient_id),
                        MessageDeliveryStatus.PENDING.value,
                    )
                    for recipient_id in recipient_ids
                ),
            )
            return self._get_message(connection, message.message_id)

    def get_message(self, message_id: UUID) -> StoredChatMessage:
        with self.database.connect() as connection:
            return self._get_message(connection, message_id)

    def list_messages(
        self,
        room_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[StoredChatMessage, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connect() as connection:
            self._require_room_row(connection, room_id)
            rows = connection.execute(
                """
                SELECT message_id FROM chat_messages
                WHERE room_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (str(room_id), after_sequence, limit),
            ).fetchall()
            return tuple(
                self._get_message(connection, UUID(row["message_id"])) for row in rows
            )

    def pending_for(
        self,
        member_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[StoredChatMessage, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM room_members WHERE member_id = ?", (str(member_id),)
            ).fetchone() is None:
                raise MemberNotFoundError(f"room member not found: {member_id}")
            rows = connection.execute(
                """
                SELECT m.message_id FROM chat_messages m
                JOIN chat_deliveries d ON d.message_id = m.message_id
                WHERE d.recipient_id = ? AND d.status = ? AND m.sequence > ?
                ORDER BY m.sequence LIMIT ?
                """,
                (
                    str(member_id),
                    MessageDeliveryStatus.PENDING.value,
                    after_sequence,
                    limit,
                ),
            ).fetchall()
            return tuple(
                self._get_message(connection, UUID(row["message_id"])) for row in rows
            )

    def acknowledge(self, message_id: UUID, *, recipient_id: UUID) -> MessageDelivery:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM chat_deliveries
                WHERE message_id = ? AND recipient_id = ?
                """,
                (str(message_id), str(recipient_id)),
            ).fetchone()
            if row is None:
                raise InvalidChatAcknowledgementError(
                    "only a resolved message recipient may acknowledge it"
                )
            if row["status"] == MessageDeliveryStatus.PENDING.value:
                connection.execute(
                    """
                    UPDATE chat_deliveries SET status = ?, acknowledged_at = ?
                    WHERE message_id = ? AND recipient_id = ?
                    """,
                    (
                        MessageDeliveryStatus.ACKNOWLEDGED.value,
                        utc_now().isoformat(),
                        str(message_id),
                        str(recipient_id),
                    ),
                )
            updated = connection.execute(
                """
                SELECT * FROM chat_deliveries
                WHERE message_id = ? AND recipient_id = ?
                """,
                (str(message_id), str(recipient_id)),
            ).fetchone()
        return _delivery_from_row(updated)

    def get_thread(self, message_id: UUID) -> tuple[StoredChatMessage, ...]:
        with self.database.connect() as connection:
            current = self._get_message(connection, message_id)
            seen: set[UUID] = set()
            while current.message.reply_to is not None:
                if current.message.message_id in seen:
                    raise TeamRoomStoreError("reply chain contains a cycle")
                seen.add(current.message.message_id)
                current = self._get_message(connection, current.message.reply_to)
            root_id = current.message.message_id
            rows = connection.execute(
                """
                WITH RECURSIVE thread(message_id) AS (
                    SELECT message_id FROM chat_messages WHERE message_id = ?
                    UNION ALL
                    SELECT child.message_id FROM chat_messages child
                    JOIN thread parent ON child.reply_to = parent.message_id
                )
                SELECT m.message_id FROM chat_messages m
                JOIN thread t ON t.message_id = m.message_id
                ORDER BY m.sequence
                """,
                (str(root_id),),
            ).fetchall()
            return tuple(
                self._get_message(connection, UUID(row["message_id"])) for row in rows
            )

    @staticmethod
    def _insert_member(connection: sqlite3.Connection, member: RoomMember) -> None:
        connection.execute(
            """
            INSERT INTO room_members(member_id, room_id, name, role, kind, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(member.member_id),
                str(member.room_id),
                member.name,
                member.role.value,
                member.kind.value,
                member.joined_at.isoformat(),
            ),
        )

    def _get_room(self, connection: sqlite3.Connection, room_id: UUID) -> TeamRoom:
        row = self._require_room_row(connection, room_id)
        members = connection.execute(
            "SELECT * FROM room_members WHERE room_id = ? ORDER BY joined_at, member_id",
            (str(room_id),),
        ).fetchall()
        return TeamRoom(
            room_id=row["room_id"],
            task_id=row["task_id"],
            trace_id=row["trace_id"],
            name=row["name"],
            status=row["status"],
            members=tuple(_member_from_row(member) for member in members),
            created_at=datetime.fromisoformat(row["created_at"]),
            closed_at=(
                datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None
            ),
        )

    @staticmethod
    def _require_room_row(connection: sqlite3.Connection, room_id: UUID) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM team_rooms WHERE room_id = ?", (str(room_id),)
        ).fetchone()
        if row is None:
            raise RoomNotFoundError(f"team room not found: {room_id}")
        return row

    def _get_message(
        self, connection: sqlite3.Connection, message_id: UUID
    ) -> StoredChatMessage:
        row = connection.execute(
            "SELECT * FROM chat_messages WHERE message_id = ?", (str(message_id),)
        ).fetchone()
        if row is None:
            raise ChatMessageNotFoundError(f"chat message not found: {message_id}")
        deliveries = connection.execute(
            """
            SELECT * FROM chat_deliveries
            WHERE message_id = ? ORDER BY recipient_id
            """,
            (str(message_id),),
        ).fetchall()
        return StoredChatMessage(
            sequence=row["sequence"],
            message=ChatMessage.model_validate_json(row["message_json"]),
            deliveries=tuple(_delivery_from_row(item) for item in deliveries),
        )


def _member_from_row(row: sqlite3.Row) -> RoomMember:
    return RoomMember(
        member_id=row["member_id"],
        room_id=row["room_id"],
        name=row["name"],
        role=row["role"],
        kind=row["kind"],
        joined_at=datetime.fromisoformat(row["joined_at"]),
    )


def _delivery_from_row(row: sqlite3.Row) -> MessageDelivery:
    return MessageDelivery(
        message_id=row["message_id"],
        recipient_id=row["recipient_id"],
        status=row["status"],
        acknowledged_at=(
            datetime.fromisoformat(row["acknowledged_at"])
            if row["acknowledged_at"]
            else None
        ),
    )


def _fingerprint(message: ChatMessage, recipient_ids: tuple[UUID, ...]) -> str:
    content = message.model_dump(mode="json", exclude={"message_id", "created_at"})
    content["resolved_recipient_ids"] = sorted(str(item) for item in recipient_ids)
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
