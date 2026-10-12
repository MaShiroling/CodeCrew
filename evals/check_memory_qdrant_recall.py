"""Opt-in real Qdrant chat recall with synthetic cache/query vectors only.

No paid embedding or Agent, real chat DB, service stop or default/config edit.
Creates/cleans one random owned versioned collection and isolated SQLite.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.chat.dispatch import chat_context
from app.chat.models import StandaloneChatMessage
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorAnchor, VectorBackendError, VectorModel, VectorRecord
from app.chat.vector_outbox import MemoryVectorOutbox
from app.config import Settings
from app.storage import SQLiteDatabase
from app.team import MemberRole
from evals.check_memory_vector_maintenance import ROOT

RESULTS = ROOT / "evals/results/memory-qdrant-recall-v1"


class SyntheticQueryEmbedder:
    model_fingerprint = "codecrew-qdrant-recall-probe-v1"
    def __init__(self):
        self.calls, self.fail = 0, False
    def embed(self, texts):
        if self.fail:
            raise RuntimeError("synthetic embedding failure")
        self.calls += len(texts)
        return tuple((1., 0., 0.) for _ in texts)


def probe(*, output_root=RESULTS):
    settings = Settings()
    endpoint = QdrantEndpoint(base_url=settings.memory_qdrant_url,
        collection_prefix="codecrew_recall_probe_" + uuid4().hex[:24],
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds)
    embedder = SyntheticQueryEmbedder()
    backend = QdrantVersionedVectorBackend(endpoint, VectorModel(embedder.model_fingerprint, 3))
    original_request = backend._request
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {"complete": False, "cleanup_complete": False, "collection": backend.collection_name,
              "checks": [], "embedding_calls": 0, "agent_calls": 0,
              "data": "synthetic cached/query vectors; isolated canonical DB; real Qdrant reads/writes",
              "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                  "app/chat/qdrant_memory_index.py", "app/chat/vector_backend_factory.py",
                  "app/chat/qdrant_vector_sync.py", "app/chat/qdrant_vector_backend.py", "app/chat/memory.py",
                  "app/chat/dispatch.py", "app/cli.py", "evals/check_memory_qdrant_recall.py")}}
    attempted = False
    try:
        if backend._collection() is not None:
            raise RuntimeError("owned collection collision")
        attempted = True
        backend.initialize()
        database = SQLiteDatabase(output / "synthetic.sqlite3")
        store = StandaloneChatStore(database, lexical_backend="bm25")
        store.initialize()
        if not store.memory.fts_index.status()["usable"]:
            raise RuntimeError("actual FTS5 required for this probe")
        service = StandaloneChatService(store)
        room = service.create_room(title="synthetic hybrid recall", idempotency_key=uuid4()).room_id
        other_room = service.create_room(title="synthetic other", idempotency_key=uuid4()).room_id
        worker = MemoryVectorOutbox(database, backend)
        worker.register_target()
        records = {}
        def save(text, vector, target_room=room):
            source = service.post_message(target_room, content="@白金 " + text,
                                          idempotency_key=uuid4(), reply_to=None).message
            memory = service.create_memory(target_room, source_message_id=source.message_id, content=text)
            anchor = VectorAnchor(memory.memory_id, target_room, source.message_id, source.correlation_id,
                                  hashlib.sha256(text.encode()).hexdigest())
            record = VectorRecord(anchor, backend.model, vector)
            SQLiteMemoryVectorBackend(database).upsert((record,))
            records[memory.memory_id] = record
            return memory
        lexical = save("gateway 读超时为 8 秒", (0., 1., 0.))
        semantic = save("异步任务采用消息队列", (1., 0., 0.))
        save("平时喜欢温水", (0., 0., 1.))
        current = save("仅属于当前讨论", (1., 0., 0.))
        save("其他房间不得读", (1., 0., 0.), other_room)
        retired = save("被撤销的旧记忆", (1., 0., 0.))
        worker.run_once()
        store.memory.retire(room, retired.memory_id)  # Keep old remote point to test canonical filtering.
        correlation = records[current.memory_id].anchor.source_correlation_id
        index = QdrantMemoryVectorIndex(database, embedder, endpoint, backend_factory=lambda model: backend)
        store.memory.vector_index = index
        retrieve = lambda query: store.memory.retrieve(room, query, exclude_correlation_id=correlation)
        if retrieve("gateway") != (lexical, semantic) or retrieve("后台如何解耦") != (semantic,):
            raise RuntimeError("scoped hybrid fusion failed")
        room_record = store.get_room(room)
        planner = next(member for member in room_record.members if member.role is MemberRole.PLANNER)
        human = next(member for member in room_record.members if member.role is MemberRole.HUMAN)
        agent_reply = store.append_message(StandaloneChatMessage(
            room_id=room, trace_id=room_record.trace_id, sender_id=planner.member_id,
            recipient_ids=(human.member_id,), content="合成 Agent 回执（未调用模型）", idempotency_key=str(uuid4()),
            reply_to=current.source_message_id, correlation_id=correlation))
        query = service.post_message(room, content="@月见 后台如何解耦", idempotency_key=uuid4(),
                                     reply_to=agent_reply.message.message_id)
        context = json.loads(chat_context(store, query, store.get_room(room)))
        if [item["memory_id"] for item in context["room_memories"]] != [str(semantic.memory_id)]:
            raise RuntimeError("chat context injection not canonical scoped")
        report["checks"].append("real_scoped_semantic_rrf_and_chat_context_injection")

        SQLiteMemoryVectorBackend(database).upsert((records[semantic.memory_id],))
        if retrieve("后台如何解耦"):
            raise RuntimeError("pending revision leaked old remote point")
        worker.run_once()
        if retrieve("后台如何解耦") != (semantic,):
            raise RuntimeError("explicit delivery did not restore recall")
        report["checks"].append("pending_revision_fence_and_explicit_delivery_restore")

        def fail_request(*args, **kwargs):
            raise VectorBackendError("injected unavailable")
        backend._request = fail_request
        calls_before = embedder.calls
        if retrieve("gateway") != (lexical,) or embedder.calls != calls_before:
            raise RuntimeError("storage failure did not cheaply degrade to BM25")
        backend._request = original_request
        embedder.fail = True
        if retrieve("gateway") != (lexical,):
            raise RuntimeError("embedding failure did not degrade to BM25")
        embedder.fail = False
        store.memory.fts_index.invalidate("SyntheticFailure")
        if retrieve("gateway") != (lexical, semantic):
            raise RuntimeError("FTS failure did not preserve semantic branch")
        store.memory.fts_index.initialize(force_rebuild=True)
        report["checks"].append("injected_storage_embedding_fts_failures_use_correct_fallback")
        report["fault_scope"] = "injected failures, not an actual Qdrant process outage"

        original_search = backend.search_current
        def retire_after_read(request, mutations):
            hits = original_search(request, mutations)
            store.memory.retire(room, semantic.memory_id)
            return hits
        backend.search_current = retire_after_read
        if retrieve("后台如何解耦"):
            raise RuntimeError("concurrent retirement leaked remote hit")
        backend.search_current = original_search
        restarted = StandaloneChatStore(SQLiteDatabase(database.path), vector_index=index, lexical_backend="bm25")
        restarted.initialize()
        if restarted.memory.retrieve(room, "后台如何解耦", exclude_correlation_id=correlation):
            raise RuntimeError("restart resurrected retired semantic fact")
        report["checks"].append("canonical_revalidation_during_remote_read_and_sqlite_restart")
        report["outbox_status"] = worker.status()
        report["recall_checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - safe category only, never raw provider response
        report["error_type"] = type(exc).__name__
    finally:
        backend._request = original_request  # Cleanup must not inherit an injected failure.
        if attempted:
            try:
                if backend._request("DELETE", backend._path) is not True or backend._collection() is not None:
                    raise RuntimeError("owned cleanup incomplete")
                report["cleanup_complete"] = True
            except Exception as exc:  # noqa: BLE001 - safe category only
                report["cleanup_error_type"] = type(exc).__name__
        report["synthetic_query_vector_requests"] = embedder.calls
        report["complete"] = bool(report.get("recall_checks_passed") and report["cleanup_complete"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: --live uses isolated synthetic SQLite and one random owned Qdrant collection; "
              "checks scoped chat recall/RRF/revisions/revalidation/injected fallbacks then cleans only that collection. "
              "No paid embedding/Agent, real chat database, default change or service stop.")
        return
    try:
        output, report = probe()
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"Qdrant recall probe failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Recall probe incomplete; inspect safe report\n")
    print("Real Qdrant scoped chat recall, injected fallbacks and owned cleanup passed; paid embedding/Agent 0/0")


if __name__ == "__main__":
    main()
