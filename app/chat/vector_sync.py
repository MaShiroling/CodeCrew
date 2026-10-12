"""Explicit one-shot/watch Qdrant outbox operator; never auto-started by chat-serve.

Default is plan only. --status is read-only. --run requires an existing migrated
database and an explicit model identity; it reuses cached embeddings only.
--watch additionally requires the worker enable flag and a prepared target. It
supervises a delivery thread inside this standalone process; timeout exits 1.
"""

import argparse
import json
from pathlib import Path

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker import VectorWorkerStop, vector_worker_signals, watch_vector_worker
from app.chat.vector_worker_contracts import requested_vector_worker_policy
from app.chat.vector_worker_diagnostics import safe_worker_diagnostics
from app.config import Settings
from app.storage import SQLiteDatabase


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--watch", action="store_true", help="explicit continuous delivery of an already prepared target")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--model-fingerprint")
    parser.add_argument("--dimensions", type=int)
    parser.add_argument("--collection-prefix", help="explicit target namespace override; does not change private settings")
    parser.add_argument("--initialize-target", action="store_true",
                        help="explicitly initialize remote collection and subscribe future changes")
    parser.add_argument("--retry-dead", action="store_true", help="bounded explicit redrive before delivery")
    parser.add_argument("--limit", type=int, help="one-shot --run limit (default 32); watch uses worker batch settings")
    args = parser.parse_args(argv)
    if (args.initialize_target or args.retry_dead) and not args.run:
        parser.error("target initialization/redrive requires --run")
    if args.watch and args.limit is not None:
        parser.error("--watch uses configured worker batch size, not --limit")
    if not args.run and not args.status and not args.watch:
        print("Plan only: --run delivers a bounded Qdrant outbox batch from an explicitly selected "
              "existing database/model, using cached vectors; zero embedding/Agent calls. "
              "--initialize-target creates a model-specific sync collection and subscribes future changes. "
              "--status reads counts only; no network. --watch also requires the worker enable setting, "
              "uses an already prepared target and never initializes/redrives. No defaults/config/database opened.")
        return
    if not args.database or not args.database.is_file() or not args.model_fingerprint or args.dimensions is None:
        parser.error("existing --database, --model-fingerprint and --dimensions required")
    limit = 32 if args.limit is None else args.limit
    if not 1 <= limit <= 200:
        parser.error("--limit must be within 1–200")
    try:
        settings = Settings()
        policy = requested_vector_worker_policy(settings, requested=args.watch)
        backend = QdrantVersionedVectorBackend(QdrantEndpoint(
            base_url=settings.memory_qdrant_url,
            collection_prefix=args.collection_prefix or settings.memory_qdrant_collection_prefix,
            api_key=settings.memory_qdrant_api_key.get_secret_value(),
            timeout_seconds=settings.memory_qdrant_timeout_seconds,
        ), VectorModel(args.model_fingerprint, args.dimensions))
        if args.watch:
            stop = VectorWorkerStop()
            with vector_worker_signals(stop):
                result = watch_vector_worker(args.database, backend, policy, stop=stop)
            print(json.dumps({"reason": result.reason, "attempted": result.attempted,
                              "clean_shutdown": result.exit_code == 0, "diagnostics": result.diagnostics,
                              "diagnostics_status": result.diagnostics_status, "embedding_calls": 0, "agent_calls": 0}))
            if result.exit_code:
                parser.exit(result.exit_code, "Vector watch exited without clean shutdown\n")
            return
        worker = MemoryVectorOutbox(SQLiteDatabase(args.database), backend)
        # Require schema before creating remote resources. No migration here.
        counts = worker.status()
        attempted = redriven = 0
        if args.run:
            if args.initialize_target:
                backend.initialize()
                worker.register_target()
            if args.retry_dead:
                redriven = worker.retry_dead(limit=limit)
            attempted = worker.run_once(limit=limit)
            counts = worker.status()
        print(json.dumps({"attempted": attempted, "redriven": redriven, "status": counts,
                          **safe_worker_diagnostics(worker),
                          "embedding_calls": 0, "agent_calls": 0}))
    except Exception as exc:  # noqa: BLE001 - print safe category, not secrets/provider bodies
        parser.exit(1, f"Vector sync failed: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
