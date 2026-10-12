"""Prepare one new persistent real-acceptance DB/namespace, never start it.

Plan by default. Explicit --prepare loads current credentials without printing
or copying them. Dimensions come from validated same-fingerprint cloud evidence,
not a paid request. Never seed old facts/cache, deliver events or modify .env.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorModel
from app.chat.vector_memory import EmbeddingEndpoint
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import check_vector_worker_target
from app.config import Settings
from app.storage import SQLiteDatabase
from evals.run_memory_fts_ab import SharedEmbeddings
from scripts.backup_memory_live_database import backup_database, inventory

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / ".codecrew/memory-live-acceptance"
CACHE = ROOT / "evals/results/memory-fts-ab-v1/20261011T113351Z-8f218a82/embedding-cache.json"


def dimension_evidence(settings, path):
    if settings.memory_embedding_mode != "on":
        raise ValueError("configured_embedding_not_enabled")
    endpoint = EmbeddingEndpoint(settings.memory_embedding_base_url, settings.memory_embedding_model,
        settings.memory_embedding_revision, settings.memory_embedding_api_key.get_secret_value(),
        settings.memory_embedding_timeout_seconds)
    path = Path(path).resolve(strict=True)
    cache, identity = SharedEmbeddings.load(path)
    if (cache.model_fingerprint != endpoint.model_fingerprint or identity != {
            "model": endpoint.model, "revision": endpoint.revision}
            or cache.dimensions is None or not cache.vectors):
        raise ValueError("current_model_and_cloud_evidence_mismatch")
    return VectorModel(endpoint.model_fingerprint, cache.dimensions), {
        "basis": "validated_historical_cloud_cache_same_fingerprint",
        "cache": str(path), "cache_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "validated_vectors": len(cache.vectors), "identity": identity,
        "current_credentials_verified": False, "new_embedding_calls": 0,
    }


def prepare_target(settings, *, cache_path=CACHE, output_root=TARGETS,
                   backend_factory=QdrantVersionedVectorBackend):
    # Validate evidence/endpoint before creating any persistent resource.
    model, evidence = dimension_evidence(settings, cache_path)
    endpoint = QdrantEndpoint(base_url=settings.memory_qdrant_url,
        collection_prefix="codecrew_live_real_" + uuid4().hex[:16],
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds)
    url = urlsplit(endpoint.base_url)
    if url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("selected_local_qdrant_required")
    backend = backend_factory(endpoint, model)
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(mode=0o700, exist_ok=False)
    database_path = output / "chat.sqlite3"
    fd = os.open(database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    overrides = {
        "database_url": "sqlite:///" + str(database_path),
        "memory_lexical_backend": "bm25", "memory_vector_store": "qdrant",
        "memory_qdrant_url": endpoint.base_url, "memory_qdrant_collection_prefix": endpoint.collection_prefix,
        "memory_embedding_mode": "on", "memory_vector_worker_enabled": False,
        "standalone_chat_workspace_root": str(output / "workspaces"),
        "standalone_chat_runtime_root": str(output / "runtime"),
    }
    report = {
        "version": 1, "complete": False, "phase": "allocated", "directory": str(output),
        "database": str(database_path), "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_fingerprint": model.fingerprint, "dimensions": model.dimensions,
        "collection": backend.collection_name, "target_id": backend.sync_target_id,
        "dimension_evidence": evidence, "runtime_overrides": overrides,
        "worker_started": False, "chat_started": False, "private_env_changed": False,
        "embedding_calls": 0, "agent_calls": 0, "old_data_imported": False,
    }
    def checkpoint():
        temporary = output / "prepare.tmp.json"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        temporary.replace(output / "prepare.json")  # Only our manifest in a new owned private directory.
    checkpoint()
    try:
        database = SQLiteDatabase(database_path)
        store = StandaloneChatStore(database, lexical_backend="bm25")
        store.initialize()  # This newly allocated empty DB only, never existing user DB.
        if not store.memory.fts_index.status()["usable"]:
            raise RuntimeError("new_database_bm25_unavailable")
        report["phase"] = "schema_prepared"
        checkpoint()
        # UUID namespace collision fails closed; never repair/reuse another collection.
        if backend._collection() is not None:
            raise RuntimeError("dedicated_collection_already_exists")
        report["phase"] = "creating_dedicated_collection"
        checkpoint()
        backend.initialize()
        worker = MemoryVectorOutbox(database, backend)
        worker.register_target()  # Subscribe future changes only. No claims or historical backfill.
        report["phase"] = "target_registered"
        checkpoint()
        prepared = check_vector_worker_target(database_path, backend)
        snapshot = inventory(database_path)
        if (snapshot["schema_version"] != 29 or snapshot["counts"].get("standalone_chat_vector_targets") != 1
                or any(snapshot["counts"].get(table, 0) for table in (
                    "standalone_chat_rooms", "standalone_chat_messages", "standalone_chat_memories",
                    "standalone_chat_memory_embeddings", "standalone_chat_vector_outbox"))):
            raise RuntimeError("new_target_not_empty_or_current")
        if worker.status() or worker.diagnostics()["backlog_total"] != 0:
            raise RuntimeError("new_target_has_backlog")
        # Empty cache is deliberate: no historical eval vectors or facts enter this DB.
        report.update(inventory=snapshot, fts_usable=True, startup_preflight_passed=True,
                      target_identity_matches=prepared.target_id == backend.sync_target_id,
                      backlog_total=0, cache_state="empty_waits_for_real_writer",
                      remote_points=backend._request("GET", backend._path)["points_count"])
        if report["remote_points"] != 0:
            raise RuntimeError("dedicated_collection_not_empty")
        backup_output, backup = backup_database(database_path, output_root=output / "backups")
        if not backup["complete"]:
            raise RuntimeError("prepared_target_backup_failed")
        report.update(backup_manifest=str(backup_output / "manifest.json"), phase="ready_not_started", complete=True)
    except Exception as exc:  # noqa: BLE001 - no credential/provider/SQL output
        report["error_type"] = type(exc).__name__
        report["resources_retained_for_review"] = True  # No destructive cleanup or retry into old namespaces.
    checkpoint()
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--embedding-cache", type=Path, default=CACHE)
    args = parser.parse_args(argv)
    if not args.prepare:
        print("Plan only: new private persistent DB/schema 29 + dedicated Qdrant namespace/target + readonly preflight "
              "+ backup; validated same-model historical dimensions; empty facts/cache. "
              "No Settings/.env opened, old DB imported, worker/chat started, paid model or default changes.")
        return
    try:
        output, report = prepare_target(Settings(), cache_path=args.embedding_cache)
    except Exception as exc:  # noqa: BLE001 - no Settings validation details/secrets
        parser.exit(1, f"Preparation refused before allocation: {type(exc).__name__}\n")
    print(f"Preparation report: {output / 'prepare.json'}")
    if not report["complete"]:
        parser.exit(1, "Target preparation incomplete; resources retained, worker/chat not started\n")
    print("Dedicated persistent target prepared; empty cache/backlog; worker/chat not started; models 0/0")


if __name__ == "__main__":
    main()
