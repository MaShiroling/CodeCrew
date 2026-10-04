"""SQLite ingress reservations and ordered delivery, never Agent execution."""

import sqlite3
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from app.chat.discussion_runs import DiscussionRun, DiscussionRunStatus
from app.chat.models import StandaloneChatMessage
from app.chat.store import StandaloneChatStore
from app.feishu.models import FeishuInbound
from app.orchestration.models import utc_now
from app.storage import Migration

ACTIVE_RUNS = {DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING, DiscussionRunStatus.PAUSED}

FEISHU_MIGRATIONS = (Migration(version=19, name="create_feishu_bridge", statements=(
    """CREATE TABLE feishu_bindings (
        app_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE REFERENCES standalone_chat_rooms(room_id),
        status TEXT NOT NULL CHECK(status IN ('active','disabled')),
        start_sequence INTEGER NOT NULL, scan_cursor INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(app_id, chat_id))""",
    """CREATE TABLE feishu_ingress (
        ingress_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, event_id TEXT NOT NULL,
        message_id TEXT NOT NULL, chat_id TEXT NOT NULL, sender_open_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
        human_message_id TEXT, correlation_id TEXT, run_id TEXT, opening_role TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('received','admitted','busy','rejected','interrupted')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(app_id, event_id), UNIQUE(app_id, message_id),
        FOREIGN KEY(app_id, chat_id) REFERENCES feishu_bindings(app_id, chat_id))""",
    """CREATE TABLE feishu_ingress_events (
        app_id TEXT NOT NULL, event_id TEXT NOT NULL,
        ingress_id TEXT NOT NULL REFERENCES feishu_ingress(ingress_id),
        PRIMARY KEY(app_id, event_id))""",
    """CREATE TABLE feishu_outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('agent','status','ingress')),
        source_key TEXT NOT NULL, internal_message_id TEXT,
        sequence INTEGER NOT NULL, text TEXT NOT NULL, reply_to_message_id TEXT,
        ingress_id TEXT NOT NULL REFERENCES feishu_ingress(ingress_id),
        status TEXT NOT NULL CHECK(status IN ('pending','sending','retry_wait','sent','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT,
        sending_at TEXT, last_error TEXT, receipt_message_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(app_id, source_kind, source_key),
        FOREIGN KEY(app_id, chat_id) REFERENCES feishu_bindings(app_id, chat_id))""",
    "CREATE INDEX feishu_outbox_delivery ON feishu_outbox(app_id, chat_id, sequence, outbox_id)",
    "CREATE INDEX feishu_ingress_correlation ON feishu_ingress(room_id, correlation_id)",
)),)


def platform_key(app_id: str, kind: str, identifier: str) -> UUID:
    # Length-prefixed components avoid delimiter ambiguity in external IDs.
    return uuid5(NAMESPACE_URL, f"codecrew:feishu:{len(app_id)}:{app_id}:{kind}:{identifier}")


class FeishuConflict(ValueError):
    """A replay changed identity/content. Messages contain no platform data."""


class FeishuStore:
    def __init__(self, chat: StandaloneChatStore, app_id: str) -> None:
        self.chat = chat
        self.database = chat.database
        self.app_id = app_id

    def initialize(self) -> None:
        self.chat.initialize()
        self.database.initialize(FEISHU_MIGRATIONS)

    def binding(self, chat_id: str) -> sqlite3.Row | None:
        with self.database.connect() as conn:
            return conn.execute("SELECT * FROM feishu_bindings WHERE app_id=? AND chat_id=?",
                                (self.app_id, chat_id)).fetchone()

    def bind(self, chat_id: str, room_id: UUID) -> sqlite3.Row:
        with self.database.transaction() as conn:
            start = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM standalone_chat_messages WHERE room_id=?",
                (str(room_id),),
            ).fetchone()[0]
            now = utc_now().isoformat()
            conn.execute("""INSERT OR IGNORE INTO feishu_bindings
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
                         (self.app_id, chat_id, str(room_id), start, start, now, now))
            row = conn.execute("SELECT * FROM feishu_bindings WHERE app_id=? AND chat_id=?",
                               (self.app_id, chat_id)).fetchone()
            if row is None or row["room_id"] != str(room_id) or row["status"] != "active":
                raise FeishuConflict("binding_conflict")
            return row

    def claim(self, event: FeishuInbound, room_id: UUID, opening_role: str) -> sqlite3.Row:
        with self.database.transaction() as conn:
            by_event = conn.execute("""SELECT i.* FROM feishu_ingress_events e
                JOIN feishu_ingress i ON i.ingress_id=e.ingress_id
                WHERE e.app_id=? AND e.event_id=?""", (self.app_id, event.event_id)).fetchone()
            by_message = conn.execute("SELECT * FROM feishu_ingress WHERE app_id=? AND message_id=?",
                                      (self.app_id, event.message_id)).fetchone()
            for row in (by_event, by_message):
                if row is not None and (
                    row["message_id"] != event.message_id or row["fingerprint"] != event.fingerprint()
                ):
                    raise FeishuConflict("ingress_identity_conflict")
            existing = by_event or by_message
            ingress_id = (existing["ingress_id"] if existing is not None else
                          str(platform_key(self.app_id, "message", event.message_id)))
            if existing is None:
                now = utc_now().isoformat()
                conn.execute("""INSERT INTO feishu_ingress
                    (ingress_id, app_id, event_id, message_id, chat_id, sender_open_id, fingerprint,
                     room_id, opening_role, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?)""",
                             (ingress_id, self.app_id, event.event_id, event.message_id,
                              event.chat_id, event.sender_open_id, event.fingerprint(),
                              str(room_id), opening_role, now, now))
            conn.execute("INSERT OR IGNORE INTO feishu_ingress_events VALUES (?, ?, ?)",
                         (self.app_id, event.event_id, ingress_id))
            return conn.execute("SELECT * FROM feishu_ingress WHERE ingress_id=?",
                                (ingress_id,)).fetchone()

    def finish_ingress(
        self, ingress_id: str, status: str, *, message: StandaloneChatMessage | None = None,
        run: DiscussionRun | None = None,
    ) -> sqlite3.Row:
        with self.database.transaction() as conn:
            conn.execute("""UPDATE feishu_ingress SET status=?, human_message_id=?, correlation_id=?,
                run_id=?, updated_at=? WHERE ingress_id=? AND app_id=?""",
                         (status, str(message.message_id) if message else None,
                          str(message.correlation_id) if message else None,
                          str(run.run_id) if run else None, utc_now().isoformat(),
                          ingress_id, self.app_id))
            return conn.execute("SELECT * FROM feishu_ingress WHERE ingress_id=?",
                                (ingress_id,)).fetchone()

    def ingresses(self, *, status: str | None = None) -> list[sqlite3.Row]:
        with self.database.connect() as conn:
            return conn.execute("SELECT * FROM feishu_ingress WHERE app_id=? AND (? IS NULL OR status=?)",
                                (self.app_id, status, status)).fetchall()

    def busy(self, room_id: UUID, *, except_ingress: str) -> bool:
        with self.database.connect() as conn:
            reserved = conn.execute("""SELECT 1 FROM feishu_ingress WHERE room_id=?
                AND status='received' AND ingress_id<>? LIMIT 1""",
                                    (str(room_id), except_ingress)).fetchone()
            runs = conn.execute("SELECT run_json FROM standalone_chat_discussion_runs WHERE room_id=?",
                                (str(room_id),)).fetchall()
            return reserved is not None or any(
                DiscussionRun.model_validate_json(row[0]).status in ACTIVE_RUNS for row in runs
            )

    @staticmethod
    def enqueue(conn, ingress, *, kind: str, key: str, sequence: int,
                text: str, message_id: str | None = None) -> None:
        now = utc_now().isoformat()
        conn.execute("""INSERT OR IGNORE INTO feishu_outbox
            (app_id, chat_id, source_kind, source_key, internal_message_id, sequence, text,
             reply_to_message_id, ingress_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                     (ingress["app_id"], ingress["chat_id"], kind, key, message_id, sequence,
                      text, ingress["message_id"], ingress["ingress_id"], now, now))

    def notice(self, ingress: sqlite3.Row, text: str) -> None:
        with self.database.transaction() as conn:
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM standalone_chat_messages WHERE room_id=?",
                (ingress["room_id"],),
            ).fetchone()[0]
            self.enqueue(conn, ingress, kind="ingress", key=ingress["ingress_id"],
                         sequence=sequence, text=text)

    def recover_sending(self, *, force: bool = False) -> int:
        now = utc_now()
        with self.database.transaction() as conn:
            return conn.execute("""UPDATE feishu_outbox SET status='retry_wait', next_retry_at=?,
                last_error='delivery_result_uncertain', updated_at=?
                WHERE app_id=? AND status='sending' AND (? OR sending_at<=?)""",
                                (now.isoformat(), now.isoformat(), self.app_id, force,
                                 (now - timedelta(seconds=60)).isoformat())).rowcount

    def claim_delivery(self, *, max_attempts: int) -> sqlite3.Row | None:
        with self.database.transaction() as conn:
            # Select each chat's earliest unfinished item; a waiting head blocks later sends.
            heads = conn.execute("""SELECT o.* FROM feishu_outbox o WHERE o.app_id=?
                AND o.status NOT IN ('sent','failed') AND NOT EXISTS (
                    SELECT 1 FROM feishu_outbox p WHERE p.app_id=o.app_id AND p.chat_id=o.chat_id
                    AND p.status NOT IN ('sent','failed')
                    AND (p.sequence<o.sequence OR
                         (p.sequence=o.sequence AND p.outbox_id<o.outbox_id)))
                ORDER BY o.sequence,o.outbox_id""", (self.app_id,)).fetchall()
            now = utc_now().isoformat()
            for row in heads:
                if row["status"] == "sending" or (row["next_retry_at"] and row["next_retry_at"] > now):
                    continue
                if row["attempt_count"] >= max_attempts:
                    conn.execute("UPDATE feishu_outbox SET status='failed',updated_at=? WHERE outbox_id=?",
                                 (now, row["outbox_id"]))
                    continue
                conn.execute("""UPDATE feishu_outbox SET status='sending', attempt_count=attempt_count+1,
                    sending_at=?,updated_at=? WHERE outbox_id=?""", (now, now, row["outbox_id"]))
                return conn.execute("SELECT * FROM feishu_outbox WHERE outbox_id=?",
                                    (row["outbox_id"],)).fetchone()
            return None

    def sent(self, outbox_id: int, receipt: str) -> None:
        with self.database.transaction() as conn:
            conn.execute("""UPDATE feishu_outbox SET status='sent',receipt_message_id=?,last_error=NULL,
                updated_at=? WHERE app_id=? AND outbox_id=? AND status='sending'""",
                         (receipt, utc_now().isoformat(), self.app_id, outbox_id))

    def failed_attempt(self, row: sqlite3.Row, *, max_attempts: int, delay: float,
                       error: str = "send_failed") -> None:
        now = utc_now()
        with self.database.transaction() as conn:
            conn.execute("""UPDATE feishu_outbox SET status=?,next_retry_at=?,last_error=?,updated_at=?
                WHERE app_id=? AND outbox_id=? AND status='sending'""",
                         ("failed" if row["attempt_count"] >= max_attempts else "retry_wait",
                          (now + timedelta(seconds=delay)).isoformat(), error,
                          now.isoformat(), self.app_id, row["outbox_id"]))

    def counts(self) -> dict[str, int]:
        with self.database.connect() as conn:
            counts = dict(conn.execute("SELECT status,COUNT(*) FROM feishu_outbox WHERE app_id=? GROUP BY status",
                                       (self.app_id,)).fetchall())
            retries = conn.execute("SELECT COALESCE(SUM(MAX(attempt_count-1,0)),0) FROM feishu_outbox WHERE app_id=?",
                                   (self.app_id,)).fetchone()[0]
            bindings = conn.execute("SELECT COUNT(*) FROM feishu_bindings WHERE app_id=? AND status='active'",
                                    (self.app_id,)).fetchone()[0]
            return {
                "binding_count": bindings, "retry_count": retries,
                "pending_outbox_count": sum(counts.get(s, 0) for s in ('pending', 'sending', 'retry_wait')),
                "failed_count": counts.get('failed', 0),
            }
