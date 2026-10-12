"""Explicit checkpointed vector maintenance. Default is plan only.

No schema migration, model calls, old collection deletion or retrieval cutover.
Use --new-target with a fresh prefix for non-destructive reconstruction.
"""

import argparse
import json
from pathlib import Path
from uuid import UUID

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_outbox import MemoryVectorOutbox
from app.config import Settings
from app.storage import SQLiteDatabase


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true")
    action.add_argument("--status", action="store_true")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--initialize-target", action="store_true")
    target.add_argument("--new-target", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--model-fingerprint")
    parser.add_argument("--dimensions", type=int)
    parser.add_argument("--collection-prefix")
    parser.add_argument("--mode", choices=("backfill", "audit", "repair"))
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--deliver", action="store_true", help="explicitly deliver one bounded outbox batch after scanning")
    parser.add_argument("--findings", action="store_true", help="include bounded metadata-only findings")
    args = parser.parse_args(argv)
    if (args.initialize_target or args.new_target or args.deliver) and not args.run:
        parser.error("target writes/delivery require --run")
    if args.new_target and (args.run_id or not args.collection_prefix or args.mode not in (None, "backfill")):
        parser.error("--new-target requires a fresh --collection-prefix and a new backfill run")
    if not args.run and not args.status:
        print("Plan only: --run performs one checkpointed backfill/audit/repair page; "
              "--run-id resumes, --status reads progress without network. --new-target rebuilds "
              "into a fresh namespace, never deletes the old collection. Cached vectors only, "
              "embedding/Agent 0/0. No config/database opened.")
        return
    if not args.database or not args.database.is_file() or not args.model_fingerprint or args.dimensions is None:
        parser.error("existing --database, --model-fingerprint and --dimensions required")
    if args.status and not args.run_id:
        parser.error("--status requires --run-id")
    if not 1 <= args.limit <= 16:
        parser.error("--limit must be within 1–16")
    run_id = args.run_id
    try:
        settings = Settings()
        backend = QdrantVersionedVectorBackend(QdrantEndpoint(
            base_url=settings.memory_qdrant_url,
            collection_prefix=args.collection_prefix or settings.memory_qdrant_collection_prefix,
            api_key=settings.memory_qdrant_api_key.get_secret_value(),
            timeout_seconds=settings.memory_qdrant_timeout_seconds,
        ), VectorModel(args.model_fingerprint, args.dimensions))
        database = SQLiteDatabase(args.database)
        # Check schema 28 BEFORE creating any remote resource. No auto-migration.
        with database.connect() as conn:
            conn.execute("SELECT run_id FROM standalone_chat_vector_maintenance_runs LIMIT 0")
            registered = conn.execute("SELECT target_id FROM standalone_chat_vector_targets WHERE target_id=?",
                                      (backend.sync_target_id,)).fetchone()
        worker = MemoryVectorOutbox(database, backend)
        maintenance = MemoryVectorMaintenance(worker)
        if args.new_target and (registered is not None or backend._collection() is not None):
            raise ValueError("rebuild target already exists; choose a fresh namespace")
        if args.new_target or args.initialize_target:
            backend.initialize()
            worker.register_target()
        if run_id is None:
            run_id = maintenance.start(mode=args.mode or "backfill")
        progress = maintenance.status(run_id)
        if args.mode is not None and args.mode != progress["mode"]:
            raise ValueError("resume mode differs from saved run")
        if args.run:
            progress = maintenance.step(run_id, limit=args.limit)
        if args.deliver:
            attempted = worker.run_once(limit=args.limit)
            progress = maintenance.status(run_id)
            progress["delivery_attempted"] = attempted
        if args.findings:
            progress["findings"] = maintenance.findings(run_id, limit=args.limit)
        print(json.dumps(progress))
    except Exception as exc:  # noqa: BLE001 - safe category and own UUID only
        suffix = f"; resume run_id={run_id}" if run_id is not None else ""
        parser.exit(1, f"Vector maintenance failed: {type(exc).__name__}{suffix}\n")


if __name__ == "__main__":
    main()
