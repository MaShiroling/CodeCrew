"""Portable transactional change capture; no network or embedding calls."""

from app.storage import Migration


def _change(memory_id, room_id, tombstone):
    return f"""
        INSERT INTO standalone_chat_vector_revisions(memory_id, room_id, revision, tombstone)
        VALUES ({memory_id}, {room_id}, 1, {tombstone})
        ON CONFLICT(memory_id) DO UPDATE SET revision=revision+1, tombstone=excluded.tombstone;
        INSERT INTO standalone_chat_vector_outbox(target_id, memory_id, revision)
        SELECT t.target_id, r.memory_id, r.revision FROM standalone_chat_vector_targets t
        JOIN standalone_chat_vector_revisions r ON r.memory_id={memory_id};
    """


VECTOR_OUTBOX_MIGRATION = Migration(
    version=27, name="create_memory_vector_transactional_outbox",
    statements=(
        """CREATE TABLE standalone_chat_vector_revisions (
            memory_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
            tombstone INTEGER NOT NULL CHECK(tombstone IN (0, 1))
        )""",
        # Capture versions for old identities, but historical delivery is step 4.
        """INSERT INTO standalone_chat_vector_revisions
            SELECT memory_id, room_id, 1, status!='active' FROM standalone_chat_memories""",
        """CREATE TABLE standalone_chat_vector_targets (
            target_id TEXT PRIMARY KEY,
            model_fingerprint TEXT NOT NULL,
            dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 2 AND 4096)
        )""",
        """CREATE TABLE standalone_chat_vector_outbox (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT NOT NULL REFERENCES standalone_chat_vector_targets(target_id),
            memory_id TEXT NOT NULL REFERENCES standalone_chat_vector_revisions(memory_id),
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
                ('pending', 'processing', 'delivered', 'superseded', 'waiting', 'dead')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
            available_at REAL NOT NULL DEFAULT 0,
            lease_until REAL,
            lease_token TEXT,
            error_code TEXT,
            UNIQUE(target_id, memory_id, revision)
        )""",
        """CREATE INDEX standalone_chat_vector_outbox_ready_idx
            ON standalone_chat_vector_outbox(target_id, status, available_at, event_id)""",
        """CREATE TRIGGER standalone_chat_vector_identity_guard
            BEFORE UPDATE OF memory_id, room_id ON standalone_chat_memories
            WHEN new.memory_id!=old.memory_id OR new.room_id!=old.room_id
            BEGIN SELECT RAISE(ABORT, 'memory identity is immutable'); END""",
        "CREATE TRIGGER standalone_chat_vector_memory_ai AFTER INSERT ON standalone_chat_memories "
        "BEGIN " + _change("new.memory_id", "new.room_id", "new.status!='active'") + " END",
        """CREATE TRIGGER standalone_chat_vector_memory_au
            AFTER UPDATE OF content, status, source_message_id ON standalone_chat_memories
            WHEN new.content!=old.content OR new.status!=old.status
                OR new.source_message_id!=old.source_message_id
            BEGIN """ + _change("new.memory_id", "new.room_id", "new.status!='active'") + " END",
        "CREATE TRIGGER standalone_chat_vector_memory_ad AFTER DELETE ON standalone_chat_memories "
        "BEGIN " + _change("old.memory_id", "old.room_id", "1") + " END",
        """CREATE TRIGGER standalone_chat_vector_embedding_identity_guard
            BEFORE UPDATE OF memory_id ON standalone_chat_memory_embeddings
            WHEN new.memory_id!=old.memory_id
            BEGIN SELECT RAISE(ABORT, 'embedding identity is immutable'); END""",
        *tuple(
            f"CREATE TRIGGER standalone_chat_vector_embedding_{suffix} AFTER {operation} "
            "ON standalone_chat_memory_embeddings BEGIN "
            f"UPDATE standalone_chat_vector_revisions SET revision=revision+1 "
            f"WHERE memory_id={ref}.memory_id; "
            "INSERT INTO standalone_chat_vector_outbox(target_id, memory_id, revision) "
            "SELECT t.target_id, r.memory_id, r.revision FROM standalone_chat_vector_targets t "
            f"JOIN standalone_chat_vector_revisions r ON r.memory_id={ref}.memory_id; END"
            for suffix, operation, ref in (("ai", "INSERT", "new"), ("au", "UPDATE", "new"),
                                           ("ad", "DELETE", "old"))
        ),
        """CREATE TRIGGER standalone_chat_vector_source_au
            AFTER UPDATE OF correlation_id, message_json, sender_id, room_id ON standalone_chat_messages
            WHEN new.correlation_id!=old.correlation_id OR new.message_json!=old.message_json
                OR new.sender_id!=old.sender_id OR new.room_id!=old.room_id
            BEGIN
                UPDATE standalone_chat_vector_revisions SET revision=revision+1
                WHERE memory_id IN (SELECT m.memory_id FROM standalone_chat_memories m
                    JOIN standalone_chat_messages s ON s.message_id=m.source_message_id
                    WHERE m.source_message_id=new.message_id
                        OR s.correlation_id IN (old.correlation_id, new.correlation_id));
                INSERT INTO standalone_chat_vector_outbox(target_id, memory_id, revision)
                SELECT t.target_id, r.memory_id, r.revision FROM standalone_chat_vector_targets t
                JOIN standalone_chat_vector_revisions r ON r.memory_id IN
                    (SELECT m.memory_id FROM standalone_chat_memories m
                     JOIN standalone_chat_messages s ON s.message_id=m.source_message_id
                     WHERE m.source_message_id=new.message_id
                        OR s.correlation_id IN (old.correlation_id, new.correlation_id));
            END""",
    ),
)
