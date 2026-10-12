"""Opt-in real Qdrant maintenance, isolated canonical DB and owned collections.

Plan only by default. No real chat data, configuration edits or model calls.
--live injects drift only in two random synthetic namespaces and cleans those.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_sync_contracts import VectorMutation
from app.config import Settings
from app.storage import SQLiteDatabase

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-vector-maintenance-v1"


def finish(maintenance, run_id, *, limit=1):
    for _ in range(64):
        status = maintenance.step(run_id, limit=limit)
        if status["scan_complete"]:
            return status
    raise RuntimeError("synthetic maintenance page budget exhausted")


def probe(*, output_root=RESULTS):
    settings = Settings()
    model = VectorModel("codecrew-maintenance-probe-v1", 3)
    backends = [QdrantVersionedVectorBackend(QdrantEndpoint(
        base_url=settings.memory_qdrant_url, collection_prefix="codecrew_maint_probe_" + uuid4().hex[:24],
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds), model) for _ in range(2)]
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {"complete": False, "cleanup_complete": False, "collections": [b.collection_name for b in backends],
              "checks": [], "embedding_calls": 0, "agent_calls": 0,
              "data": "synthetic cached vectors, isolated canonical SQLite, owned collections only",
              "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                  "app/chat/vector_maintenance.py", "app/chat/vector_maintenance_contracts.py",
                  "app/chat/vector_maintenance_schema.py", "app/chat/vector_outbox.py",
                  "app/chat/vector_outbox_schema.py", "app/chat/qdrant_vector_sync.py",
                  "app/chat/qdrant_vector_backend.py", "app/chat/vector_maintain.py",
                  "evals/check_memory_vector_maintenance.py")}}
    attempted = []
    try:
        backend = backends[0]
        if backend._collection() is not None:
            raise RuntimeError("probe collection collision")
        attempted.append(backend)
        backend.initialize()
        store = StandaloneChatStore(SQLiteDatabase(output / "synthetic.sqlite3"))
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title="synthetic historical maintenance", idempotency_key=uuid4()).room_id
        memories = []
        for i in range(4):
            source = service.post_message(room, content=f"@白金 固定历史事实{i}",
                                          idempotency_key=uuid4(), reply_to=None).message
            memory = service.create_memory(room, source_message_id=source.message_id, content=f"固定历史事实{i}")
            anchor = VectorAnchor(memory.memory_id, room, source.message_id, source.correlation_id,
                                  hashlib.sha256(memory.content.encode()).hexdigest())
            SQLiteMemoryVectorBackend(store.database).upsert((VectorRecord(anchor, model, (3., 4., 0.)),))
            memories.append(memory)
        store.memory.retire(room, memories[-1].memory_id)
        facts_before = store.memory.list_active(room)
        worker = MemoryVectorOutbox(store.database, backend)
        if worker.status():
            raise RuntimeError("history unexpectedly subscribed")
        worker.register_target()
        maintenance = MemoryVectorMaintenance(worker)
        run = maintenance.start()
        first = maintenance.step(run, limit=1)
        if first["checkpoint"] != 1 or first["scan_complete"]:
            raise RuntimeError("first backfill page not bounded")
        # A new process-equivalent worker resumes durable checkpoint, no in-memory cursor.
        restarted = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend)
        maintenance = MemoryVectorMaintenance(restarted)
        final = finish(maintenance, run)
        if final["counts"].get("enqueued") != 4 or restarted.run_once() != 4:
            raise RuntimeError("historical backfill delivery incomplete")
        audit = finish(maintenance, maintenance.start(mode="audit"))
        if not audit["consistent_at_observation"] or audit["counts"].get("remote_scanned") != 4:
            raise RuntimeError("initial two-way audit incomplete")
        report["checks"].append("historical_cached_backfill_bounded_restart_and_two_way_audit")
        report["backfill"] = final
        report["initial_audit"] = audit

        with store.database.connect() as conn:
            rows = conn.execute("SELECT * FROM standalone_chat_vector_revisions ORDER BY memory_id").fetchall()
            originals = tuple(restarted.desired_in_snapshot(conn, row)[0] for row in rows)
        active = [m for m in originals if m.record is not None]
        missing, drift = active[:2]
        backend._completed(backend._request("POST", backend._path + "/points/delete?wait=true&ordering=strong",
                                            {"points": [backend.mutation_point_id(missing)]}))
        # Deliberate owned-test corruption bypasses the versioned port. Payload/hash unchanged.
        backend._completed(backend._request("PUT", backend._path + "/points?wait=true&ordering=strong", {
            "points": [{"id": backend.mutation_point_id(drift), "vector": [0., 0., 1.],
                        "payload": backend.mutation_payload(drift)}]}))
        broken = finish(maintenance, maintenance.start(mode="audit"))
        if broken["consistent_at_observation"] or broken["counts"].get("missing") != 1 or broken["counts"].get("drift") != 1:
            raise RuntimeError("real missing/vector drift not detected")
        repair = finish(maintenance, maintenance.start(mode="repair"))
        if repair["counts"].get("repair_enqueued") != 2 or restarted.run_once() != 2:
            raise RuntimeError("repair outbox delivery incomplete")
        for old in (missing, drift):
            if backend.apply_mutation(old) <= old.revision:
                raise RuntimeError("delayed old write was not fenced after repair")
        audit = finish(maintenance, maintenance.start(mode="audit"))
        if not audit["consistent_at_observation"] or store.memory.list_active(room) != facts_before:
            raise RuntimeError("repair not consistent or changed canonical facts")
        report["checks"].append("missing_and_actual_vector_drift_outbox_repair_with_old_write_fence")
        report["drift_audit"], report["repair"], report["post_repair_audit"] = broken, repair, audit

        # An orphan in the owned namespace is reported, never silently adopted/deleted.
        orphan = VectorMutation(room, uuid4(), 1, model, None)
        backend.apply_mutation(orphan)
        orphan_run = maintenance.start(mode="repair")
        orphan_audit = finish(maintenance, orphan_run)
        if orphan_audit["counts"].get("orphan") != 1 or restarted.run_once() != 0:
            raise RuntimeError("orphan handling modified local outbox")
        if backend.inspect_mutations((orphan,))[0].code != "current":
            raise RuntimeError("maintenance deleted owned orphan unexpectedly")
        report["checks"].append("remote_orphan_reported_not_adopted_or_deleted")
        report["orphan_audit"] = orphan_audit
        backend._completed(backend._request("POST", backend._path + "/points/delete?wait=true&ordering=strong",
                                            {"points": [backend.mutation_point_id(orphan)]}))

        replacement = backends[1]
        if replacement._collection() is not None:
            raise RuntimeError("rebuild collection collision")
        attempted.append(replacement)
        replacement.initialize()
        new_worker = MemoryVectorOutbox(store.database, replacement)
        new_worker.register_target()
        new_maintenance = MemoryVectorMaintenance(new_worker)
        rebuilt = finish(new_maintenance, new_maintenance.start())
        if rebuilt["counts"].get("enqueued") != 4 or new_worker.run_once() != 4:
            raise RuntimeError("new namespace cached rebuild incomplete")
        new_audit = finish(new_maintenance, new_maintenance.start(mode="audit"))
        old_audit = finish(maintenance, maintenance.start(mode="audit"))
        if not new_audit["consistent_at_observation"] or not old_audit["consistent_at_observation"]:
            raise RuntimeError("rebuild changed old namespace or is incomplete")
        report["checks"].append("fresh_namespace_rebuild_reuses_cache_preserves_old_no_cutover")
        report["rebuild"], report["rebuilt_audit"] = rebuilt, new_audit
        report["maintenance_checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - no provider body/URL/key in report
        report["error_type"] = type(exc).__name__
    finally:
        cleaned = []
        for owned in attempted:
            try:
                if owned._request("DELETE", owned._path) is not True or owned._collection() is not None:
                    raise RuntimeError("owned probe cleanup incomplete")
                cleaned.append(owned.collection_name)
            except Exception as exc:  # noqa: BLE001 - safe category only
                report["cleanup_error_type"] = type(exc).__name__
        report["cleaned_collections"] = cleaned
        report["cleanup_complete"] = len(cleaned) == len(attempted) == 2
        report["complete"] = bool(report.get("maintenance_checks_passed") and report["cleanup_complete"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: --live uses an isolated synthetic SQLite and two random owned Qdrant collections; "
              "checks historical backfill/restart/audit/drift repair/orphan/rebuild then cleans those only. "
              "Embedding/Agent 0/0; no real chat database or retrieval cutover.")
        return
    try:
        output, report = probe()
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"Vector maintenance probe failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Maintenance probe incomplete; inspect safe report\n")
    print("Real Qdrant backfill/audit/repair/rebuild and owned cleanup passed; embedding/Agent 0/0")


if __name__ == "__main__":
    main()
