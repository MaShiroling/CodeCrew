"""Actual SQLite orchestration with fake remote storage, no network/model calls."""

import hashlib
import json
import struct
import urllib.request
from dataclasses import replace
from uuid import uuid4

import pytest

from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorHit,
    VectorModel,
    VectorRecord,
)
from app.chat.vector_memory import MemoryVectorIndex, _cosine
from app.chat.vector_outbox import MemoryVectorOutbox
from app.cli import _build_chat_service
from app.config import Settings
from app.storage import SQLiteDatabase


class OfflineEmbedder:
    model_fingerprint = "offline-recall-v1"
    def __init__(self):
        self.calls, self.fail, self.hook = [], False, None
        self.vector = (1., 0.)
    def embed(self, texts):
        self.calls.append(tuple(texts))
        if self.fail:
            raise RuntimeError("private-provider-body")
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return tuple(self.vector for _ in texts)


class VersionedFake:
    model = VectorModel("offline-recall-v1", 2)
    sync_target_id = "c" * 64
    def __init__(self):
        self.points, self.calls = {}, []
        self.fail, self.hook, self.bad = False, None, None
    def _require_sync_ready(self):
        self.calls.append("ready")
        if self.fail:
            raise VectorBackendError("synthetic unavailable")
    def apply_mutation(self, mutation):
        current = self.points.get(mutation.memory_id)
        if current is None or current.revision < mutation.revision:
            self.points[mutation.memory_id] = mutation
        return self.points[mutation.memory_id].revision
    def search_current(self, request, mutations):
        self.calls.append((request, mutations))
        hits = tuple(VectorHit(m.record.anchor, request.model, _cosine(request.vector, m.record.vector))
                     for m in mutations if self.points.get(m.memory_id) == m
                     and _cosine(request.vector, m.record.vector) >= request.min_similarity)
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return self.bad(hits, request) if self.bad else hits


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline embedding call"))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("offline Qdrant call"))
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "recall.sqlite3"), lexical_backend="bm25")
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="recall", idempotency_key=uuid4()).room_id
    backend, embedder = VersionedFake(), OfflineEmbedder()
    index = QdrantMemoryVectorIndex(store.database, embedder, QdrantEndpoint(), backend_factory=lambda model: backend)
    worker = MemoryVectorOutbox(store.database, backend)
    worker.register_target()
    return store, service, room, backend, embedder, index, worker


def save(setup, text, *, room=None, vector=(1., 0.), **kwargs):
    store, service, default_room, *_ = setup
    room = room or default_room
    source = service.post_message(room, content="@白金 " + text, idempotency_key=uuid4(), reply_to=None).message
    memory = service.create_memory(room, source_message_id=source.message_id, content=text, **kwargs)
    if vector is not None:
        anchor = VectorAnchor(memory.memory_id, room, source.message_id, source.correlation_id,
                              hashlib.sha256(text.encode()).hexdigest())
        SQLiteMemoryVectorBackend(store.database).upsert((VectorRecord(anchor, VectorModel("offline-recall-v1", 2), vector),))
    return memory


def activate(setup):
    setup[-1].run_once(limit=200)
    setup[0].memory.vector_index = setup[-2]


@pytest.mark.parametrize("lexical", ["legacy", "bm25"])
def test_semantic_only_and_lexical_candidates_fuse_with_old_rrf(setup, lexical):
    store, _, room, backend, embedder, _, _ = setup
    store.memory.lexical_backend = lexical
    lex = save(setup, "gateway 超时 8 秒", vector=(0., 1.))
    semantic = save(setup, "异步作业采用消息队列")
    activate(setup)
    assert store.memory.retrieve(room, "gateway") == (lex, semantic)
    assert store.memory.retrieve(room, "后台处理如何解耦") == (semantic,)
    assert embedder.calls == [("gateway",), ("后台处理如何解耦",)]
    assert all(call[0].limit == 2 for call in backend.calls if isinstance(call, tuple))


def test_rrf_keeps_fourth_lexical_candidate_before_final_limit(setup):
    items = [save(setup, ("gateway " + "audit " * n).strip(), vector=(1., 0.) if n == 9 else (0., 1.))
             for n in (1, 3, 6, 9)]
    activate(setup)
    assert setup[0].memory.retrieve(setup[2], "gateway", limit=1) == (items[3],)


def test_cache_refresh_excludes_old_remote_version_until_explicit_delivery(setup):
    store, _, room, backend, embedder, index, worker = setup
    memory = save(setup, "异步队列")
    activate(setup)
    assert store.memory.retrieve(room, "后台解耦") == (memory,)
    old = backend.points[memory.memory_id]
    SQLiteMemoryVectorBackend(store.database).upsert((old.record,))
    assert store.memory.retrieve(room, "后台解耦") == () and worker.status()["pending"] == 1
    worker.run_once()
    assert store.memory.retrieve(room, "后台解耦") == (memory,)
    backend.apply_mutation(old)
    assert backend.points[memory.memory_id].revision > old.revision
    before = worker.status()
    index.search("后台解耦", (memory,))
    assert worker.status() == before and len(embedder.calls) == 4


@pytest.mark.parametrize("during", ["embedding", "remote"])
@pytest.mark.parametrize("change", ["retire", "content", "source", "correlation", "cache", "external"])
def test_concurrent_changes_are_rechecked(setup, during, change):
    store, _, room, backend, embedder, _, _ = setup
    memory = save(setup, "异步队列")
    alternate = save(setup, "其他事实", vector=(0., 1.)).source_message_id if change == "source" else None
    activate(setup)
    def mutate():
        if change == "retire":
            store.memory.retire(room, memory.memory_id)
            return
        with store.database.transaction() as conn:
            if change == "content":
                conn.execute("UPDATE standalone_chat_memories SET content='新的事实' WHERE memory_id=?", (str(memory.memory_id),))
            elif change == "source":
                conn.execute("UPDATE standalone_chat_memories SET source_message_id=? WHERE memory_id=?",
                             (str(alternate), str(memory.memory_id)))
            elif change == "correlation":
                conn.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?",
                             (str(uuid4()), str(memory.source_message_id)))
            elif change == "external":
                conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,'$.external_source','{}') "
                             "WHERE message_id=?", (str(memory.source_message_id),))
            else:
                conn.execute("UPDATE standalone_chat_memory_embeddings SET vector=? WHERE memory_id=?",
                             (struct.pack('<2f', 0, 1), str(memory.memory_id)))
    (embedder if during == "embedding" else backend).hook = mutate
    assert store.memory.retrieve(room, "后台解耦") == ()


def test_scope_excludes_room_and_current_discussion_before_remote_query(setup):
    store, service, room, backend, embedder, _, _ = setup
    allowed = save(setup, "异步队列")
    excluded = save(setup, "当前话题")
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id
    save(setup, "其他房间", room=other)
    current = store.get_message(excluded.source_message_id).message.correlation_id
    activate(setup)
    assert store.memory.retrieve(room, "后台解耦", exclude_correlation_id=current) == (allowed,)
    request = backend.calls[-1][0]
    assert request.exclude_correlation_id == current and len(request.candidates) == 1
    assert request.candidates[0].memory_id == allowed.memory_id and len(embedder.calls) == 1


@pytest.mark.parametrize("kind", ["missing", "model", "dimension", "hash", "blob", "nan", "zero", "sensitive", "external"])
def test_untrusted_candidates_skip_cloud_query(setup, kind):
    store, _, room, backend, embedder, index, _ = setup
    memory = save(setup, "异步队列")
    activate(setup)
    with store.database.transaction() as conn:
        if kind == "missing":
            conn.execute("DELETE FROM standalone_chat_memory_embeddings")
        elif kind in ("model", "dimension", "hash", "blob", "nan", "zero"):
            key, value = {"model": ("model_fingerprint", "other"), "dimension": ("dimensions", 3),
                          "hash": ("content_hash", "b" * 64), "blob": ("vector", b"bad"),
                          "nan": ("vector", struct.pack('<2f', float('nan'), 0)),
                          "zero": ("vector", struct.pack('<2f', 0, 0))}[kind]
            conn.execute(f"UPDATE standalone_chat_memory_embeddings SET {key}=?", (value,))
        else:
            key, value = ("content", "API key: synthetic-value") if kind == "sensitive" else ("external_source", "{}")
            conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,?,?)",
                         ("$." + key, value))
    assert index.search("后台解耦", (memory,)) == ()
    assert not backend.calls and not embedder.calls
    assert store.memory.retrieve(room, "后台解耦") == ()


@pytest.mark.parametrize("kind", ["backend", "embedder", "unregistered"])
def test_failure_degrades_to_selected_bm25_with_safe_logs(setup, kind, caplog):
    store, _, room, backend, embedder, _, _ = setup
    strong = save(setup, "gateway gateway gateway")
    weak = save(setup, "gateway audit audit audit audit")
    activate(setup)
    if kind == "backend":
        backend.fail = True
    elif kind == "embedder":
        embedder.fail = True
    else:
        with store.database.transaction() as conn:
            conn.execute("DELETE FROM standalone_chat_vector_outbox")
            conn.execute("DELETE FROM standalone_chat_vector_targets")
    assert store.memory.retrieve(room, "gateway") == (strong, weak)
    assert "private-provider-body" not in caplog.text and "degraded" in caplog.text
    assert bool(embedder.calls) is (kind == "embedder")


@pytest.mark.parametrize("kind", ["duplicate", "foreign", "wrong_model", "stale_anchor", "low", "not_tuple"])
def test_malformed_backend_results_fail_closed(setup, kind):
    store, _, room, backend, _, _, _ = setup
    save(setup, "异步队列")
    activate(setup)
    def bad(hits, request):
        hit = hits[0]
        return {"duplicate": hits + hits, "foreign": (replace(hit, anchor=replace(hit.anchor, memory_id=uuid4())),),
                "wrong_model": (replace(hit, model=VectorModel("other", 2)),),
                "stale_anchor": (replace(hit, anchor=replace(hit.anchor, content_hash="b" * 64)),),
                "low": (replace(hit, similarity=.1),), "not_tuple": list(hits)}[kind]
    backend.bad = bad
    assert store.memory.retrieve(room, "后台解耦") == ()


def test_index_writes_cache_outbox_only_without_remote_calls(setup):
    _, _, _, backend, embedder, index, worker = setup
    memory = save(setup, "需要生成的事实", vector=None)
    index.index((memory,))
    assert embedder.calls == [(memory.content,)] and not backend.calls and not backend.points
    assert worker.status()["pending"] == 2
    index.index((memory,))
    assert len(embedder.calls) == 1
    worker.run_once()
    assert backend.points[memory.memory_id].record is not None


def test_index_revalidates_after_provider_retires_fact(setup):
    store, _, room, _, embedder, index, _ = setup
    memory = save(setup, "需要生成的事实", vector=None)
    embedder.hook = lambda: store.memory.retire(room, memory.memory_id)
    index.index((memory,))
    with store.database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM standalone_chat_memory_embeddings").fetchone()[0] == 0


def test_actual_cli_selection_is_opt_in_no_network_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("startup network"))
    base = {"_env_file": None, "database_url": f"sqlite:///{tmp_path / 'cli.sqlite3'}",
            "memory_embedding_mode": "on", "memory_embedding_base_url": "https://example.test/v1",
            "memory_embedding_model": "offline", "memory_embedding_revision": "v1", "memory_embedding_api_key": "test-only"}
    legacy = _build_chat_service(Settings(**base))
    assert type(legacy.store.memory.vector_index) is MemoryVectorIndex
    selected = _build_chat_service(Settings(**base, memory_vector_store="qdrant"))
    assert type(selected.store.memory.vector_index) is QdrantMemoryVectorIndex
    with selected.store.database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM standalone_chat_vector_targets").fetchone()[0] == 0
    off = _build_chat_service(Settings(_env_file=None, database_url=base['database_url'],
                                     memory_vector_store="qdrant", memory_embedding_mode="off"))
    assert off.store.memory.vector_index is None


@pytest.mark.parametrize("query", ["", "API key: synthetic-value"])
def test_empty_sensitive_and_no_candidate_queries_do_not_call_cloud(setup, query):
    memory = save(setup, "异步队列")
    assert setup[-2].search(query, (memory,)) == () and setup[-2].search("问题", ()) == ()
    assert not setup[3].calls and not setup[4].calls


def test_first_embedding_establishes_provider_dimension_without_remote_setup(setup):
    store, _, _, backend, embedder, index, _ = setup
    memory = save(setup, "三维模型首次向量", vector=None)
    embedder.vector = (1., 0., 0.)
    index.index((memory,))
    with store.database.connect() as conn:
        assert conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta").fetchone()[0] == 3
    assert not backend.calls and not backend.points


def test_same_fingerprint_dimension_change_does_not_block_fact_creation(setup):
    store, _, room, _, embedder, index, _ = setup
    save(setup, "既有缓存")
    store.memory.vector_index = index
    embedder.vector = (1., 0., 0.)
    memory = save(setup, "维度错配仍保存事实", vector=None)
    assert memory in store.memory.list_active(room)
    with store.database.connect() as conn:
        assert conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM standalone_chat_memory_embeddings WHERE memory_id=?",
                            (str(memory.memory_id),)).fetchone()[0] == 0


def test_chat_context_injects_only_canonical_fact_not_remote_payload(setup):
    from app.chat.dispatch import chat_context
    store, service, room, _, _, _, _ = setup
    memory = save(setup, "异步队列")
    activate(setup)
    query = service.post_message(room, content="@月见 后台解耦", idempotency_key=uuid4(), reply_to=None)
    context = json.loads(chat_context(store, query, store.get_room(room)))
    assert [item["memory_id"] for item in context["room_memories"]] == [str(memory.memory_id)]
    assert context["room_memories"][0]["content"] == memory.content


@pytest.mark.parametrize("local_reply", [False, True])
def test_external_discussion_guard_skips_new_index_entirely(setup, local_reply):
    from app.chat.dispatch import chat_context
    from app.chat.models import ExternalChatSource, StandaloneChatMessage
    from app.team import MemberRole
    store, _, room_id, backend, embedder, _, _ = setup
    save(setup, "内部事实")
    activate(setup)
    room = store.get_room(room_id)
    human = next(member for member in room.members if member.role is MemberRole.HUMAN)
    planner = next(member for member in room.members if member.role is MemberRole.PLANNER)
    query = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=room.trace_id, sender_id=human.member_id,
        recipient_ids=(planner.member_id,), content="内部事实", idempotency_key=str(uuid4()),
        external_source=ExternalChatSource(external_chat_id="synthetic", external_sender_id="synthetic")))
    if local_reply:
        query = store.append_message(StandaloneChatMessage(
            room_id=room_id, trace_id=room.trace_id, sender_id=human.member_id, recipient_ids=(planner.member_id,),
            content="内部事实", idempotency_key=str(uuid4()), reply_to=query.message.message_id,
            correlation_id=query.message.correlation_id))
    assert json.loads(chat_context(store, query, room))["room_memories"] == []
    assert not backend.calls and not embedder.calls


def test_recall_probe_default_has_no_config_or_network(monkeypatch, capsys):
    from evals import check_memory_qdrant_recall as probe
    monkeypatch.setattr(probe, "Settings", lambda: pytest.fail("plan read private settings"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.parametrize("collision", [True, False])
def test_recall_probe_early_failure_cleans_only_owned_target(tmp_path, monkeypatch, collision):
    from evals import check_memory_qdrant_recall as probe
    instances = []
    class Backend:
        def __init__(self, endpoint, model):
            self.collection_name, self._path = endpoint.collection_prefix, "/collections/owned"
            self.exists, self.deleted = collision, False
            instances.append(self)
        def _collection(self):
            return {} if self.exists else None
        def initialize(self):
            self.exists = True
            raise OSError("private-provider-body")
        def _request(self, method, path):
            assert method == "DELETE" and path == self._path
            self.exists, self.deleted = False, True
            return True
    monkeypatch.setattr(probe, "QdrantVersionedVectorBackend", Backend)
    output, report = probe.probe(output_root=tmp_path)
    assert not report["complete"] and report["cleanup_complete"] is not collision
    assert instances[0].deleted is not collision
    assert "private-provider" not in (output / "report.json").read_text()


def test_direct_query_bounds_scope_and_mixed_room_guard(setup):
    memory = save(setup, "异步队列")
    index = setup[-2]
    with pytest.raises(ValueError):
        index.search("query", (memory,) * 201)
    with pytest.raises(ValueError):
        index.search_scoped("query", (memory,), room_id="not-a-uuid")
    other = setup[1].create_room(title="other", idempotency_key=uuid4()).room_id
    another = save(setup, "其他事实", room=other)
    with pytest.raises(ValueError):
        index.search("query", (memory, another))
    assert not setup[3].calls and not setup[4].calls
