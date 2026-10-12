"""Explicit SQLite online backup, never migrate/start/dispatch/load Settings.

Source is read-only; backup and aggregate manifest live in a new private,
Git-ignored directory. No message/fact/vector/credential output. A failed copy
stays named partial and is not a usable backup; never delete source/evidence.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / ".codecrew/backups/memory-live-target-v1"
TABLES = (
    "standalone_chat_rooms", "standalone_chat_messages", "standalone_chat_memories",
    "standalone_chat_memory_embeddings", "standalone_chat_vector_targets", "standalone_chat_vector_outbox",
)


def readonly(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("existing_database_file_required")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.execute("PRAGMA query_only=ON")
    return conn


def inventory(path):
    """One aggregate read snapshot; intentionally never fetches user content."""
    with closing(readonly(path)) as conn:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            "schema_version": conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if "schema_migrations" in tables else None,
            "counts": {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                       for table in TABLES if table in tables},
            "memory_schema_present": "standalone_chat_memories" in tables,
            "outbox_schema_present": "standalone_chat_vector_outbox" in tables,
        }


def private_json(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)


def backup_database(source, *, output_root=BACKUPS, timeout_seconds=30):
    source = Path(source).resolve(strict=True)
    if not source.is_file() or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
        raise ValueError("existing_file_and_bounded_timeout_required")
    # Do not record env/settings, raw sqlite errors, source rows or credentials.
    with closing(readonly(source)) as origin:
        output_root = Path(output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
        output.mkdir(mode=0o700, exist_ok=False)
        partial = output / "partial.sqlite3"
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        manifest = {"complete": False, "source": str(source), "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "method": "sqlite_online_backup_readonly_source", "source_migrated": False,
                    "embedding_calls": 0, "agent_calls": 0}
        try:
            deadline = time.monotonic() + timeout_seconds
            def progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("backup_budget_exhausted")
            with closing(sqlite3.connect(partial, timeout=1)) as target:
                origin.backup(target, pages=256, progress=progress, sleep=.05)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("backup_integrity_failed")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("backup_foreign_keys_failed")
            copied_inventory = inventory(partial)
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            final = output / "database.sqlite3"
            if final.exists():
                raise RuntimeError("backup_destination_collision")
            partial.rename(final)
            manifest.update(complete=True, backup=str(final), sha256=digest, inventory=copied_inventory,
                            integrity_check="ok", foreign_key_check="ok")
        except Exception as exc:  # noqa: BLE001 - keep partial, no raw private errors
            manifest["error_type"] = type(exc).__name__
        private_json(output / "manifest.json", manifest)
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", action="store_true", help="explicitly create a private online backup")
    args = parser.parse_args(argv)
    try:
        if not args.backup:
            print(json.dumps(inventory(args.database), ensure_ascii=False, indent=2))
            return
        output, manifest = backup_database(args.database)
        print(f"Backup report: {output / 'manifest.json'}")
        if not manifest["complete"]:
            parser.exit(1, "Backup incomplete; retained partial is not a usable backup\n")
        print("Backup verified; read-only source; no migration or model calls")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Target inspection failed: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
