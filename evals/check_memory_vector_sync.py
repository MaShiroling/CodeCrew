"""Opt-in real Qdrant outbox journey with an isolated synthetic SQLite database.

Plan only by default. --live creates one random sync collection and cleans only
that collection. No real chat database, Agent or embedding provider is opened.
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
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord, VectorSearch
from app.chat.vector_outbox import MemoryVectorOutbox
from app.config import Settings
from app.storage import SQLiteDatabase

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-vector-sync-v1"


def probe(*, output_root=RESULTS):
    settings = Settings()
    endpoint = QdrantEndpoint(base_url=settings.memory_qdrant_url,
                              collection_prefix="codecrew_sync_probe_" + uuid4().hex[:24],
                              api_key=settings.memory_qdrant_api_key.get_secret_value(),
                              timeout_seconds=settings.memory_qdrant_timeout_seconds)
    model = VectorModel("codecrew-sync-probe-v1", 3)
    backend = QdrantVersionedVectorBackend(endpoint, model)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = output_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    report = {"complete": False, "cleanup_complete": False, "collection": backend.collection_name,
              "checks": [], "embedding_calls": 0, "agent_calls": 0,
              "data": "synthetic vectors and isolated canonical SQLite only",
              "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                  "app/chat/vector_outbox_schema.py", "app/chat/vector_outbox.py",
                  "app/chat/vector_sync_contracts.py", "app/chat/qdrant_vector_sync.py",
                  "app/chat/qdrant_vector_backend.py", "evals/check_memory_vector_sync.py")}}
    attempted = False
    try:
        if backend._collection() is not None:
            raise RuntimeError("probe collection collision")
        attempted = True
        backend.initialize()
        store = StandaloneChatStore(SQLiteDatabase(output / "synthetic.sqlite3"))
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title="synthetic sync acceptance", idempotency_key=uuid4()).room_id
        clock = [100.0]
        worker = MemoryVectorOutbox(store.database, backend, clock=lambda: clock[0])
        worker.register_target()
        source = service.post_message(room, content="@白金 固定合成事实", idempotency_key=uuid4(), reply_to=None).message
        memory = service.create_memory(room, source_message_id=source.message_id, content="固定合成事实")
        anchor = VectorAnchor(memory.memory_id, room, source.message_id, source.correlation_id,
                              hashlib.sha256(memory.content.encode()).hexdigest())
        cached = VectorRecord(anchor, model, (1., 0., 0.))
        SQLiteMemoryVectorBackend(store.database).upsert((cached,))
        claim = worker.claim()
        original, _ = worker._materialize(claim)
        worker.deliver(claim)
        request = VectorSearch(room, model, (1., 0., 0.), (anchor,))
        if [hit.anchor for hit in backend.search(request)] != [anchor] or worker.status().get("delivered") != 1:
            raise RuntimeError("real outbox delivery failed")
        report["checks"].append("transactional_capture_cached_delivery_and_read")

        # New cache revision, remote succeeded, local ACK intentionally omitted.
        SQLiteMemoryVectorBackend(store.database).upsert((cached,))
        claim = worker.claim()
        mutation, _ = worker._materialize(claim)
        backend.apply_mutation(mutation)
        restarted = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend, clock=lambda: clock[0])
        if restarted.run_once() != 0:
            raise RuntimeError("live claim lease not respected")
        clock[0] += 61
        if restarted.run_once() != 1 or worker._finish(claim, "delivered"):
            raise RuntimeError("lease restart fencing failed")
        report["checks"].append("remote_success_before_ack_restart_and_idempotent_retry")

        store.memory.retire(room, memory.memory_id)
        if restarted.run_once() != 1 or backend.search(request):
            raise RuntimeError("retire tombstone failed")
        revision = backend.apply_mutation(original)  # Old HTTP request arrives after deletion.
        if revision <= original.revision or backend.search(request):
            raise RuntimeError("delayed old upsert resurrected vector")
        report["checks"].append("persistent_tombstone_rejects_delayed_old_write")

        second_source = service.post_message(room, content="@白金 缺失向量事实", idempotency_key=uuid4(), reply_to=None).message
        second = service.create_memory(room, source_message_id=second_source.message_id, content="缺失向量事实")
        if restarted.run_once() != 1 or restarted.status().get("waiting") != 1:
            raise RuntimeError("missing vector not honestly waiting")
        second_anchor = VectorAnchor(second.memory_id, room, second_source.message_id, second_source.correlation_id,
                                     hashlib.sha256(second.content.encode()).hexdigest())
        SQLiteMemoryVectorBackend(store.database).upsert((VectorRecord(second_anchor, model, (1., 0., 0.)),))
        if restarted.run_once() != 1 or restarted.status().get("waiting", 0):
            raise RuntimeError("new vector did not resume delivery")
        if not backend.search(VectorSearch(room, model, (1., 0., 0.), (second_anchor,))):
            raise RuntimeError("resumed active vector not searchable")
        report["checks"].append("missing_vector_tombstone_wait_and_new_revision_resume")
        report["outbox_status"] = restarted.status()
        report["sync_checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - safe category only
        report["error_type"] = type(exc).__name__
    finally:
        if attempted:
            try:
                if backend._request("DELETE", backend._path) is not True or backend._collection() is not None:
                    raise RuntimeError("probe collection cleanup incomplete")
                report["cleanup_complete"] = True
            except Exception as exc:  # noqa: BLE001 - safe category only
                report["cleanup_error_type"] = type(exc).__name__
        report["complete"] = bool(report.get("sync_checks_passed") and report["cleanup_complete"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print("Plan only: --live creates isolated synthetic SQLite and one disposable Qdrant sync collection, "
              "checks outbox/restart/tombstones then removes only that collection; embedding/Agent 0/0.")
        return
    try:
        output, report = probe()
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"Vector sync probe failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Sync probe incomplete; inspect safe report\n")
    print("Real Qdrant outbox, restart, tombstone checks and owned cleanup passed; embedding/Agent 0/0")


if __name__ == "__main__":
    main()
