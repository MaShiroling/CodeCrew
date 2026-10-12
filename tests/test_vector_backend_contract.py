"""The step-1 storage port is scoped, exact, offline, and not the CLI default."""

import hashlib
import struct
from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorHit,
    VectorModel,
    VectorRecord,
    VectorSearch,
)
from app.chat.vector_memory import MemoryVectorIndex
from app.storage import SQLiteDatabase


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("storage called embedding"))
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "contract.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="contract", idempotency_key=uuid4()).room_id
    return store, service, room, SQLiteMemoryVectorBackend(store.database)


def save(service, room, text, **kwargs):
    source = service.post_message(room, content="@白金 " + text,
                                  idempotency_key=uuid4(), reply_to=None).message
    memory = service.create_memory(room, source_message_id=source.message_id, content=text, **kwargs)
    anchor = VectorAnchor(memory.memory_id, room, source.message_id, source.correlation_id,
                          hashlib.sha256(memory.content.encode()).hexdigest())
    return memory, anchor


def request(room, *anchors, **kwargs):
    return VectorSearch(room, VectorModel("model-v1", 2), (1.0, 0.0), tuple(anchors), **kwargs)


def record(anchor, vector=(1.0, 0.0), model=None):
    return VectorRecord(anchor, model or VectorModel("model-v1", len(vector)), vector)


def test_exact_reference_and_model_scoped_upsert(setup):
    store, service, room, backend = setup
    a, aa = save(service, room, "使用中文")
    b, ba = save(service, room, "审计日志")
    records = (record(aa), record(ba, (0.0, 1.0)))
    before = store.memory.list_active(room)
    assert backend.upsert(()) == ()
    assert backend.upsert(records) == (a.memory_id, b.memory_id)
    assert backend.upsert(records) == (a.memory_id, b.memory_id)
    hits = backend.search(request(room, aa, ba))
    assert [hit.anchor.memory_id for hit in hits] == [a.memory_id]
    assert hits[0].similarity == 1
    assert store.memory.list_active(room) == before
    assert store.memory.vector_index is None  # standalone adapter is not selected
    with store.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM standalone_chat_memory_embeddings").fetchone()[0] == 2
    assert backend.search(replace(request(room, aa, ba), model=VectorModel("other-model", 2))) == ()


def test_sqlite_adapter_matches_existing_exact_baseline(setup):
    store, service, room, backend = setup
    a, aa = save(service, room, "甲任务")
    b, ba = save(service, room, "乙任务")
    backend.upsert((record(aa, (1.0, 0.1)), record(ba, (0.8, 0.6))))
    class Offline:
        model_fingerprint = "model-v1"
        def embed(self, texts):
            assert list(texts) == ["query"]
            return ((1.0, 0.0),)
    old = MemoryVectorIndex(store.database, Offline())
    expected = old.search("query", (a, b))
    actual = backend.search(request(room, aa, ba))
    assert tuple(hit.anchor.memory_id for hit in actual) == tuple(key for key, _ in expected)
    assert [hit.similarity for hit in actual] == pytest.approx([score for _, score in expected])


def test_room_delete_is_idempotent_and_cannot_delete_other_room(setup):
    store, service, room, backend = setup
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id
    _, aa = save(service, room, "甲任务")
    _, ba = save(service, other, "乙任务")
    backend.upsert((record(aa), record(ba)))
    backend.delete(room, (aa.memory_id, ba.memory_id))
    backend.delete(room, (aa.memory_id, ba.memory_id))
    assert backend.search(request(room, aa)) == ()
    assert len(backend.search(request(other, ba))) == 1
    assert len(store.memory.list_active(room)) == len(store.memory.list_active(other)) == 1


def test_retired_or_replaced_snapshot_cannot_repopulate_index(setup):
    store, service, room, backend = setup
    memory, anchor = save(service, room, "原方案")
    backend.upsert((record(anchor),))
    save(service, room, "新方案", replaces_memory_id=memory.memory_id)
    assert backend.upsert((record(anchor),)) == ()
    assert backend.search(request(room, anchor)) == ()
    assert store.memory.list_active(room)[0].content == "新方案"


@pytest.mark.parametrize("field,value", [
    ("content_hash", "a" * 64), ("room_id", UUID(int=8)),
    ("source_message_id", UUID(int=9)), ("source_correlation_id", UUID(int=10)),
])
def test_stale_write_anchor_skipped_without_canonical_changes(setup, field, value):
    store, service, room, backend = setup
    _, anchor = save(service, room, "原事实")
    before = store.memory.list_active(room)
    assert backend.upsert((record(replace(anchor, **{field: value})),)) == ()
    assert store.memory.list_active(room) == before


def test_stale_read_metadata_is_revalidated_before_limit(setup):
    store, service, room, backend = setup
    _, aa = save(service, room, "甲任务")
    _, ba = save(service, room, "乙任务")
    backend.upsert((record(aa), record(ba, (0.9, 0.1))))
    forged = replace(aa, source_correlation_id=uuid4())
    # First/highest row is rejected before the limit, so the next valid row survives.
    hits = backend.search(request(room, forged, ba, limit=1))
    assert [hit.anchor for hit in hits] == [ba]
    excluded = aa.source_correlation_id
    with store.database.transaction() as connection:
        connection.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?",
                           (str(excluded), str(ba.source_message_id)))
    assert backend.search(request(room, ba, exclude_correlation_id=excluded)) == ()


@pytest.mark.parametrize("kind", ["content_hash", "blob", "nan", "dimension", "model"])
def test_derived_drift_does_not_leak_hits(setup, kind):
    store, service, room, backend = setup
    _, aa = save(service, room, "甲任务")
    backend.upsert((record(aa),))
    with store.database.transaction() as connection:
        if kind == "content_hash":
            connection.execute("UPDATE standalone_chat_memory_embeddings SET content_hash=?", ("b" * 64,))
        elif kind == "blob":
            connection.execute("UPDATE standalone_chat_memory_embeddings SET vector=?", (b"bad",))
        elif kind == "nan":
            connection.execute("UPDATE standalone_chat_memory_embeddings SET vector=?",
                               (struct.pack("<2f", float("nan"), 1.0),))
        elif kind == "dimension":
            connection.execute("UPDATE standalone_chat_memory_embeddings SET dimensions=3")
        else:
            connection.execute("UPDATE standalone_chat_memory_embeddings SET model_fingerprint='other'")
    assert backend.search(request(room, aa)) == ()
    if kind == "blob":
        # SQLite dynamic typing may admit TEXT with the expected BLOB length.
        with store.database.transaction() as connection:
            connection.execute("UPDATE standalone_chat_memory_embeddings SET vector='notablob'")
        assert backend.search(request(room, aa)) == ()


def test_model_dimension_change_fails_atomically(setup):
    _, service, room, backend = setup
    _, aa = save(service, room, "甲任务")
    _, ba = save(service, room, "乙任务")
    backend.upsert((record(aa),))
    with pytest.raises(VectorBackendError, match="dimensions"):
        backend.upsert((record(ba), record(aa, (1.0, 0.0, 0.0))))
    assert backend.search(request(room, ba)) == ()
    with pytest.raises(VectorBackendError, match="dimensions"):
        backend.search(replace(request(room, aa), model=VectorModel("model-v1", 3), vector=(1.0, 0.0, 0.0)))
    assert len(backend.search(request(room, aa))) == 1


def test_allowlist_ties_threshold_and_empty_success(setup):
    _, service, room, backend = setup
    _, aa = save(service, room, "甲任务")
    _, ba = save(service, room, "乙任务")
    backend.upsert((record(ba), record(aa)))
    hits = backend.search(request(room, aa, ba))
    assert [hit.anchor.memory_id for hit in hits] == sorted((aa.memory_id, ba.memory_id), key=str)
    assert backend.search(request(room, ba, limit=1))[0].anchor == ba
    assert backend.search(request(room)) == ()
    assert len(backend.search(request(room, aa, min_similarity=1))) == 1


def test_backend_unavailable_is_not_an_empty_result(tmp_path):
    backend = SQLiteMemoryVectorBackend(SQLiteDatabase(tmp_path / "missing.sqlite3"))
    anchor = VectorAnchor(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64)
    with pytest.raises(VectorBackendError, match="search failed"):
        backend.search(request(anchor.room_id, anchor))
    with pytest.raises(VectorBackendError, match="write failed"):
        backend.upsert((record(anchor),))
    with pytest.raises(VectorBackendError, match="delete failed"):
        backend.delete(anchor.room_id, (anchor.memory_id,))


@pytest.mark.parametrize("change", [
    {"dimensions": True}, {"dimensions": 1}, {"dimensions": 4097},
    {"fingerprint": ""}, {"metric": "dot"},
])
def test_invalid_model_rejected(change):
    with pytest.raises(ValueError):
        VectorModel(**{"fingerprint": "model", "dimensions": 2, **change})


@pytest.mark.parametrize("vector", [(float("inf"), 0), (float("nan"), 0), (True, 0),
                                     (1e100, 0), (1.0,), [1.0, 0.0]])
def test_invalid_vectors_rejected(vector):
    anchor = VectorAnchor(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64)
    with pytest.raises(ValueError):
        VectorRecord(anchor, VectorModel("model", 2), vector)
    with pytest.raises(ValueError):
        VectorSearch(anchor.room_id, VectorModel("model", 2), vector, (anchor,))


@pytest.mark.parametrize("change", [{"limit": 0}, {"limit": 201}, {"limit": True},
                                     {"min_similarity": float("nan")}, {"min_similarity": 1.1},
                                     {"min_similarity": True}])
def test_invalid_query_bounds(change):
    with pytest.raises(ValueError):
        request(uuid4(), **change)


def test_scope_and_candidate_bounds_fail_closed():
    anchor = VectorAnchor(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64)
    for anchors, kwargs in (((anchor, anchor), {}), ((anchor,), {"room_id": uuid4()}),
                            ((anchor,), {"exclude_correlation_id": anchor.source_correlation_id}),
                            (tuple(replace(anchor, memory_id=uuid4()) for _ in range(201)), {})):
        with pytest.raises(ValueError):
            request(kwargs.pop("room_id", anchor.room_id), *anchors, **kwargs)
    with pytest.raises(ValueError):
        replace(anchor, status="retired")
    with pytest.raises(ValueError):
        replace(anchor, content_hash="not-sha256")
    with pytest.raises(FrozenInstanceError):
        anchor.room_id = uuid4()


def test_invalid_hit_and_storage_batches_rejected(setup):
    _, service, room, backend = setup
    _, aa = save(service, room, "甲任务")
    with pytest.raises(ValueError):
        VectorHit(aa, VectorModel("model", 2), float("nan"))
    with pytest.raises(ValueError):
        backend.upsert((record(aa), record(aa)))
    with pytest.raises(ValueError):
        backend.upsert(tuple(record(replace(aa, memory_id=uuid4())) for _ in range(33)))
    with pytest.raises(ValueError):
        backend.delete(room, tuple(uuid4() for _ in range(201)))
    with pytest.raises(TypeError):
        backend.search(None)
