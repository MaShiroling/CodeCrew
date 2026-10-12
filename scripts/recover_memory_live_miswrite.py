"""One reviewed miswrite only: backup, retire, one delivery, bounded audit.

Plan opens nothing; --check is read-only; --apply is explicitly authorized
repair, not a new live score. No initialization, model, retry, or daemon.
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.chat.memory import ChatMemoryStore
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker import _ExistingWorkerDatabase
from app.chat.vector_worker_contracts import check_vector_worker_target
from evals.memory_persistent_resume import _snapshot, database_snapshot, digest, target_lock
from scripts.backup_memory_live_database import backup_database, private_json, readonly
from scripts.run_memory_live_worker import load_target, require

REVIEWED_RUN = "20261012T123657Z-c4c65c5c"
REVIEWED_SHA = "56b324fc7e453fa3bea15148948f6d737aecd1eb7a95804e22d9a88ba79ee28d"
BAD_ID = UUID("1722e891-ca99-4a49-96e9-8675d7494083")
PRESERVED = ("schema_migrations", "standalone_chat_messages", "standalone_chat_rooms",
    "standalone_chat_members", "standalone_chat_turns", "standalone_chat_deliveries",
    "standalone_chat_memory_candidates", "standalone_chat_embedding_meta")


def preserved(path):
    with closing(readonly(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        body = {table: sorted((dict(r) for r in conn.execute('SELECT * FROM "' + table + '"')), key=digest)
                for table in PRESERVED}
        for table in ("standalone_chat_memories", "standalone_chat_memory_embeddings", "standalone_chat_vector_revisions"):
            body[table] = sorted((dict(r) for r in conn.execute('SELECT * FROM "' + table + '" WHERE memory_id<>?',
                                                              (str(BAD_ID),))), key=digest)
        return digest(body)


def inspect(target):
    path = target.directory / "live-runs" / REVIEWED_RUN / "report.json"
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == REVIEWED_SHA, "reviewed_report_changed")
    report = json.loads(raw)
    require(report["manifest_sha256"] == target.manifest_sha256 and report["database"] == str(target.database)
        and report["worker_cleanup_complete"] and report["worker_exit"] == 0, "reviewed_target_mismatch")
    require(report["database_snapshot"] == database_snapshot(target.database), "reviewed_database_changed")
    with closing(readonly(target.database)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_memories WHERE memory_id=?", (str(BAD_ID),)).fetchone()
        require(row is not None and row["source_message_id"] == report["steps"][11]["source"]
            and row["room_id"] == report["rooms"]["personal"] and row["kind"] == "preference"
            and row["origin"] == "agent_auto" and row["content"] == "没有记录就说明未知"
            and row["status"] == "active", "reviewed_memory_mismatch")
        backlog = conn.execute("SELECT * FROM standalone_chat_vector_outbox WHERE status IN ('pending','processing','waiting','dead')").fetchall()
        require(len(backlog) == 2 and all(r["memory_id"] == str(BAD_ID) and r["target_id"] == target.backend.sync_target_id
            and r["status"] == "pending" and r["attempts"] == 0 for r in backlog), "unexpected_recovery_backlog")
        room = UUID(row["room_id"])
    check_vector_worker_target(target.database, target.backend)  # Read-only remote metadata, no allocations.
    return path, room, report["database_snapshot"]


class GuardedDatabase(_ExistingWorkerDatabase):
    def __init__(self, path, expected):
        super().__init__(path)
        self.expected = expected

    @contextmanager
    def transaction(self, *, immediate=True):
        with super().transaction(immediate=immediate) as conn:
            if self.expected is not None:
                require(_snapshot(conn) == self.expected, "database_changed_before_retirement")
            yield conn
        self.expected = None  # Only after the production retirement transaction committed.


def repair(target):
    with target_lock(target.directory):
        parent, room, before = inspect(target)
        unchanged = preserved(target.database)
        directory = target.directory / "recovery-runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        report = {"complete": False, "live_acceptance_passed": False, "operation": "reviewed_single_memory_retirement",
            "memory_id": str(BAD_ID), "manifest": str(target.manifest), "manifest_sha256": target.manifest_sha256,
            "parent_report": str(parent), "parent_report_sha256": REVIEWED_SHA,
            "target_id": target.backend.sync_target_id, "before": before, "phase": "preflight_passed",
            "agent_calls": 0, "embedding_calls": 0, "background_worker_started": False}
        def checkpoint():
            temp = directory / (uuid4().hex + ".tmp.json")
            private_json(temp, report)
            temp.replace(directory / "report.json")
        checkpoint()
        try:
            backup_dir, backup = backup_database(target.database, output_root=directory / "backup")
            report["backup_manifest"] = str(backup_dir / "manifest.json")
            require(backup["complete"] and database_snapshot(Path(backup["backup"])) == before, "recovery_backup_failed_or_changed")
            report["phase"] = "backup_verified"
            checkpoint()
            database = GuardedDatabase(target.database, before)
            memory = ChatMemoryStore(database, lexical_backend="bm25").retire(room, BAD_ID)
            require(memory is not None and memory.status == "retired" and memory.retired_reason == "manual", "retirement_failed")
            report["phase"] = "retired"
            checkpoint()
            worker = MemoryVectorOutbox(database, target.backend)
            report["delivery_attempted"] = worker.run_once(limit=1)
            report["outbox_status"] = worker.status()
            require(report["delivery_attempted"] == 1 and not any(report["outbox_status"].get(k, 0)
                for k in ("pending", "processing", "waiting", "dead")), "recovery_delivery_incomplete")
            maintenance = MemoryVectorMaintenance(worker)
            run = maintenance.start(mode="audit")
            report.update(phase="audit_started", audit_run_id=str(run))
            checkpoint()
            for _ in range(8):
                status = maintenance.step(run, limit=16)
                report["audit"] = status
                checkpoint()
                if status["scan_complete"]:
                    break
            require(status["scan_complete"] and status["consistent_at_observation"], "recovery_audit_incomplete")
            with database.connect() as conn:
                conn.execute("BEGIN")
                row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?", (str(BAD_ID),)).fetchone()
                mutation, waiting = worker.desired_in_snapshot(conn, row)
                require(waiting is None and mutation.record is None, "retired_record_not_tombstone")
                require(conn.execute("SELECT COUNT(*) FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(BAD_ID),)).fetchone()[0] == 0
                    and conn.execute("SELECT COUNT(*) FROM standalone_chat_memory_fts_documents WHERE memory_id=?", (str(BAD_ID),)).fetchone()[0] == 0,
                    "retired_cache_or_fts_remains")
            observed = target.backend.inspect_mutations((mutation,))[0]
            require(observed.code == "current" and observed.remote_revision == mutation.revision, "remote_tombstone_not_current")
            report.update(remote_tombstone_revision=mutation.revision, preserved_data_unchanged=preserved(target.database) == unchanged)
            require(report["preserved_data_unchanged"] and hashlib.sha256(parent.read_bytes()).hexdigest() == REVIEWED_SHA
                and hashlib.sha256(target.manifest.read_bytes()).hexdigest() == target.manifest_sha256, "preserved_evidence_changed")
            report.update(complete=True, phase="recovered_and_audited")
        except Exception as exc:  # noqa: BLE001 - retain backup/evidence, no raw SQL/provider/key output
            report["error_type"] = type(exc).__name__
        finally:
            report["after"] = database_snapshot(target.database)
            checkpoint()
        return directory, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.check and not args.apply:
        print("Plan only: one pinned miswrite; backup + retire + one cached/tombstone delivery + audit; no model, initialization or retry.")
        return
    if args.manifest is None:
        parser.error("explicit manifest required")
    try:
        target = load_target(args.manifest)
        if args.check:
            inspect(target)
            print("Reviewed target/memory/backlog verified; read-only; agent/embedding=0/0")
            return
        directory, report = repair(target)
        print("Recovery report: " + str(directory / "report.json"))
        if not report["complete"]:
            parser.exit(1, "Recovery incomplete; backup retained; no automatic retry\n")
        print("One memory retired; backlog zero; audit consistent; old live result unchanged; agent/embedding=0/0")
    except Exception as exc:  # noqa: BLE001 - fixed type only
        parser.exit(1, "Recovery refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
