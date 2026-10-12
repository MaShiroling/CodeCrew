"""Transactional, rebuildable FTS5 derivative of canonical room memories.

A dirty/unavailable index must never be consumed. BM25 reads are opt-in and
return canonical identities only, within the caller's bounded snapshot.
"""

import hashlib
import logging
import math
import sqlite3
from uuid import UUID

from app.chat.fts_text import (
    FTS_TEXT_REVISION,
    FTSTextLimitError,
    build_fts_query,
    encode_fts_document,
)
from app.orchestration.models import utc_now
from app.storage import Migration, SQLiteDatabase

FTS_MEMORY_MIGRATION = Migration(
    version=26, name="create_standalone_chat_fts_derivative_metadata",
    # Portable tables only. Optional FTS5 DDL cannot block the legacy runtime.
    statements=(
        """CREATE TABLE standalone_chat_memory_fts_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            revision TEXT,
            status TEXT NOT NULL CHECK(status IN ('ready', 'dirty', 'unavailable')),
            error_type TEXT,
            updated_at TEXT NOT NULL
        )""",
        """INSERT INTO standalone_chat_memory_fts_state
            (singleton, status, updated_at) VALUES (1, 'dirty', '')""",
        """CREATE TABLE standalone_chat_memory_fts_documents (
            rowid INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE REFERENCES standalone_chat_memories(memory_id),
            room_id TEXT NOT NULL,
            source_correlation_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            revision TEXT NOT NULL,
            tokens TEXT NOT NULL
        )""",
        ("CREATE INDEX standalone_chat_memory_fts_document_room_idx "
         "ON standalone_chat_memory_fts_documents(room_id, source_correlation_id)"),
    ),
)

_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS standalone_chat_memory_fts USING fts5(
    tokens, content='standalone_chat_memory_fts_documents', content_rowid='rowid', tokenize='ascii'
)"""
_TRIGGERS = {
    "standalone_chat_memory_fts_ai": """AFTER INSERT ON standalone_chat_memory_fts_documents
        BEGIN INSERT INTO standalone_chat_memory_fts(rowid, tokens)
        VALUES (new.rowid, new.tokens); END""",
    "standalone_chat_memory_fts_ad": """AFTER DELETE ON standalone_chat_memory_fts_documents
        BEGIN INSERT INTO standalone_chat_memory_fts(standalone_chat_memory_fts, rowid, tokens)
        VALUES ('delete', old.rowid, old.tokens); END""",
    "standalone_chat_memory_fts_au": """AFTER UPDATE ON standalone_chat_memory_fts_documents
        BEGIN INSERT INTO standalone_chat_memory_fts(standalone_chat_memory_fts, rowid, tokens)
        VALUES ('delete', old.rowid, old.tokens);
        INSERT INTO standalone_chat_memory_fts(rowid, tokens)
        VALUES (new.rowid, new.tokens); END""",
}
_CANONICAL = (
    "SELECT memory.memory_id, memory.room_id, source.correlation_id, memory.content "
    "FROM standalone_chat_memories AS memory JOIN standalone_chat_messages AS source "
    "ON source.message_id = memory.source_message_id WHERE memory.status = 'active'"
)
_LOGGER = logging.getLogger(__name__)


class MemoryFTSIndex:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.revision = FTS_TEXT_REVISION

    def _state(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM standalone_chat_memory_fts_state WHERE singleton = 1",
        ).fetchone()

    def _mark(
        self, connection: sqlite3.Connection, status: str, error_type: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO standalone_chat_memory_fts_state "
            "(singleton, revision, status, error_type, updated_at) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
            "status=excluded.status, error_type=excluded.error_type, updated_at=excluded.updated_at",
            (self.revision, status, error_type, utc_now().isoformat()),
        )

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(_FTS_DDL)
        self._create_triggers(connection)

    @staticmethod
    def _create_triggers(connection: sqlite3.Connection) -> None:
        for name, body in _TRIGGERS.items():
            # IF NOT EXISTS preserves a same-named stale/no-op definition.
            # Reinstall only our three derivative triggers in the caller's
            # transaction, so a healthy snapshot also stays healthy on writes.
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            connection.execute(f"CREATE TRIGGER {name} {body}")

    def _document(self, row: sqlite3.Row) -> tuple[str, ...]:
        content = row["content"]
        return (
            row["memory_id"], row["room_id"], row["correlation_id"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(), self.revision,
            encode_fts_document(content),
        )

    def _documents_current(self, connection: sqlite3.Connection) -> bool:
        # Startup audit is intentionally exact for the current small local store.
        desired = {row["memory_id"]: self._document(row)
                   for row in connection.execute(_CANONICAL)}
        actual = {
            row["memory_id"]: tuple(row[key] for key in (
                "memory_id", "room_id", "source_correlation_id", "content_hash", "revision", "tokens",
            )) for row in connection.execute("SELECT * FROM standalone_chat_memory_fts_documents")
        }
        return desired == actual

    @staticmethod
    def _integrity_check(connection: sqlite3.Connection) -> None:
        # rank=1 checks both internal FTS integrity and external content agreement.
        connection.execute(
            "INSERT INTO standalone_chat_memory_fts(standalone_chat_memory_fts, rank) "
            "VALUES ('integrity-check', 1)",
        )

    def _rebuild(self, connection: sqlite3.Connection) -> None:
        # Replace only the derived document projection. Disabling these three
        # triggers avoids deleting tokens from an already missing/corrupt index.
        for name in _TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        connection.execute("DELETE FROM standalone_chat_memory_fts_documents")
        connection.executemany(
            "INSERT INTO standalone_chat_memory_fts_documents "
            "(memory_id, room_id, source_correlation_id, content_hash, revision, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._document(row) for row in connection.execute(_CANONICAL)),
        )
        connection.execute(
            "INSERT INTO standalone_chat_memory_fts(standalone_chat_memory_fts) VALUES ('rebuild')",
        )
        self._create_triggers(connection)
        self._integrity_check(connection)

    def initialize(self, *, force_rebuild: bool = False) -> bool:
        """Conditionally create/audit/backfill; never migrate a user DB via a CLI here."""
        with self.database.transaction() as connection:
            connection.execute("SAVEPOINT memory_fts_initialize")
            try:
                self._create_schema(connection)
                state = self._state(connection)
                rebuild = (force_rebuild or state is None or state["status"] != "ready"
                           or state["revision"] != self.revision
                           or not self._documents_current(connection))
                if not rebuild:
                    try:
                        self._integrity_check(connection)
                    except sqlite3.DatabaseError:
                        rebuild = True
                if rebuild:
                    self._rebuild(connection)
                self._mark(connection, "ready")
            except (sqlite3.Error, FTSTextLimitError) as exc:
                connection.execute("ROLLBACK TO memory_fts_initialize")
                connection.execute("RELEASE memory_fts_initialize")
                status = ("unavailable" if "no such module: fts5" in str(exc) else "dirty")
                self._mark(connection, status, type(exc).__name__)
                _LOGGER.warning("memory FTS initialize degraded: %s", type(exc).__name__)
                return False
            connection.execute("RELEASE memory_fts_initialize")
            return True

    def status(self) -> dict:
        """Safe status only; no raw memory text or encoded private tokens."""
        with self.database.connect() as connection:
            row = self._state(connection)
            state = (dict(row) if row is not None else {
                "status": "dirty", "revision": None, "error_type": "MissingMetadata",
            })
            state["current_revision"] = self.revision
            state["usable"] = state["status"] == "ready" and state["revision"] == self.revision
            state["document_count"] = connection.execute(
                "SELECT COUNT(*) FROM standalone_chat_memory_fts_documents",
            ).fetchone()[0]
            return state

    def search(
        self, connection: sqlite3.Connection, room_id: UUID, query: str, *,
        memory_ids: tuple[UUID, ...], exclude_correlation_id: UUID | None = None,
    ) -> tuple[UUID, ...] | None:
        """None means unavailable, () means a valid search with no matches.

        Read state, canonical candidates and FTS within one caller-owned read
        transaction. Return the entire bounded lexical ranking, not just top 3,
        so RRF can reward a document present further down both rankings.
        """
        if not connection.in_transaction:
            raise ValueError("FTS search requires a read snapshot")
        if len(memory_ids) > 200:
            raise ValueError("FTS candidate limit exceeded")
        state = self._state(connection)
        if state is None or state["status"] != "ready" or state["revision"] != self.revision:
            return None
        match = build_fts_query(query)
        if match is None or not memory_ids:
            return ()
        placeholders = ",".join("?" for _ in memory_ids)
        rows = connection.execute(
            "SELECT memory.memory_id, bm25(standalone_chat_memory_fts) AS score "
            "FROM standalone_chat_memory_fts "
            "JOIN standalone_chat_memory_fts_documents doc "
            "ON doc.rowid = standalone_chat_memory_fts.rowid "
            "JOIN standalone_chat_memories memory ON memory.memory_id = doc.memory_id "
            "JOIN standalone_chat_messages source ON source.message_id = memory.source_message_id "
            "WHERE standalone_chat_memory_fts MATCH ? "
            "AND memory.room_id = ? AND memory.status = 'active' "
            "AND (? IS NULL OR source.correlation_id != ?) "
            "AND doc.room_id = memory.room_id AND doc.source_correlation_id = source.correlation_id "
            "AND doc.revision = ? "
            f"AND memory.memory_id IN ({placeholders}) "
            "ORDER BY score ASC, memory.created_at DESC, memory.memory_id DESC LIMIT 200",
            (match, str(room_id),
             str(exclude_correlation_id) if exclude_correlation_id else None,
             str(exclude_correlation_id) if exclude_correlation_id else None,
             self.revision, *(str(memory_id) for memory_id in memory_ids)),
        ).fetchall()
        if any(not math.isfinite(row["score"]) for row in rows):
            raise sqlite3.DatabaseError("nonfinite BM25 score")
        return tuple(UUID(row["memory_id"]) for row in rows)

    def invalidate(self, error_type: str) -> None:
        """Persist a query failure only after releasing the read snapshot."""
        with self.database.transaction() as connection:
            self._mark(connection, "dirty", error_type)

    def sync(self, connection: sqlite3.Connection, *memory_ids: UUID) -> bool:
        """Use caller's canonical transaction. Index failure marks dirty atomically.

        Savepoint rollback leaves canonical changes intact, but never labels a
        partially updated projection ready. If even invalidation fails, the
        exception rolls back the outer transaction rather than silently lying.
        """
        if not connection.in_transaction:
            raise ValueError("FTS sync requires the canonical transaction")
        state = self._state(connection)
        if state is None:
            self._mark(connection, "dirty", "MissingMetadata")
            return False
        if state["status"] != "ready":
            return False
        if state["revision"] != self.revision:
            self._mark(connection, "dirty", "RevisionMismatch")
            return False
        connection.execute("SAVEPOINT memory_fts_sync")
        try:
            for memory_id in dict.fromkeys(memory_ids):
                self._sync_one(connection, memory_id)
        except (sqlite3.Error, FTSTextLimitError) as exc:
            connection.execute("ROLLBACK TO memory_fts_sync")
            connection.execute("RELEASE memory_fts_sync")
            self._mark(connection, "dirty", type(exc).__name__)
            _LOGGER.warning("memory FTS sync degraded: %s", type(exc).__name__)
            return False
        connection.execute("RELEASE memory_fts_sync")
        return True

    def _sync_one(self, connection: sqlite3.Connection, memory_id: UUID) -> None:
        row = connection.execute(
            _CANONICAL + " AND memory.memory_id = ?", (str(memory_id),),
        ).fetchone()
        if row is None:
            connection.execute(
                "DELETE FROM standalone_chat_memory_fts_documents WHERE memory_id = ?",
                (str(memory_id),),
            )
            return
        connection.execute(
            "INSERT INTO standalone_chat_memory_fts_documents "
            "(memory_id, room_id, source_correlation_id, content_hash, revision, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(memory_id) DO UPDATE SET "
            "room_id=excluded.room_id, source_correlation_id=excluded.source_correlation_id, "
            "content_hash=excluded.content_hash, revision=excluded.revision, tokens=excluded.tokens "
            "WHERE room_id != excluded.room_id "
            "OR source_correlation_id != excluded.source_correlation_id "
            "OR content_hash != excluded.content_hash OR revision != excluded.revision "
            "OR tokens != excluded.tokens",
            self._document(row),
        )
