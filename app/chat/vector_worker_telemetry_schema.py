"""Transactional, metadata-only enqueue/ACK observations. No invented history."""

from app.storage import Migration

VECTOR_WORKER_TELEMETRY_MIGRATION = Migration(
    version=29, name="observe_memory_vector_event_times",
    statements=(
        """CREATE TABLE standalone_chat_vector_event_times (
            event_id INTEGER PRIMARY KEY REFERENCES standalone_chat_vector_outbox(event_id) ON DELETE CASCADE,
            enqueued_at REAL CHECK(enqueued_at>=0),
            delivered_at REAL CHECK(delivered_at>=0)
        )""",
        """CREATE TRIGGER standalone_chat_vector_event_time_ai
            AFTER INSERT ON standalone_chat_vector_outbox BEGIN
            INSERT INTO standalone_chat_vector_event_times(event_id,enqueued_at)
            VALUES (new.event_id,CAST(strftime('%s','now') AS REAL)); END""",
        """CREATE TRIGGER standalone_chat_vector_event_time_au
            AFTER UPDATE OF status ON standalone_chat_vector_outbox
            WHEN new.status='delivered' AND old.status!='delivered' BEGIN
            INSERT INTO standalone_chat_vector_event_times(event_id,delivered_at)
            VALUES (new.event_id,CAST(strftime('%s','now') AS REAL))
            ON CONFLICT(event_id) DO UPDATE SET delivered_at=excluded.delivered_at; END""",
    ),
)
