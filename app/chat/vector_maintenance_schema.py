from app.storage import Migration

VECTOR_MAINTENANCE_MIGRATION = Migration(
    version=28, name="create_memory_vector_maintenance_checkpoints",
    statements=(
        """CREATE TABLE standalone_chat_vector_maintenance_runs (
            run_id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL REFERENCES standalone_chat_vector_targets(target_id),
            mode TEXT NOT NULL CHECK(mode IN ('backfill', 'audit', 'repair')),
            high_water TEXT NOT NULL,
            local_cursor TEXT NOT NULL DEFAULT '',
            remote_cursor TEXT,
            phase TEXT NOT NULL DEFAULT 'local' CHECK(phase IN ('local', 'remote', 'done')),
            checkpoint INTEGER NOT NULL DEFAULT 0,
            start_event_id INTEGER NOT NULL,
            counts_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT
        )""",
        """CREATE TABLE standalone_chat_vector_maintenance_findings (
            run_id TEXT NOT NULL REFERENCES standalone_chat_vector_maintenance_runs(run_id),
            phase TEXT NOT NULL,
            item_id TEXT NOT NULL,
            code TEXT NOT NULL,
            remote_revision INTEGER,
            PRIMARY KEY(run_id, phase, item_id, code)
        )""",
    ),
)
