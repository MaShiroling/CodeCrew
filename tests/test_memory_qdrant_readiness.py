"""Real REST adapter/SQLite orchestration with fake HTTP, never actual network."""

import io
import json
import urllib.error
import urllib.request
from dataclasses import replace
from uuid import uuid4

import pytest

from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_backend import PAYLOAD_SCHEMA, QdrantEndpoint, QdrantMemoryVectorBackend
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorBackendError, VectorModel, VectorSearch
from app.chat.vector_outbox import MemoryVectorOutbox
from app.storage import SQLiteDatabase
from evals.qdrant_readiness_reference import RepeatedReadinessBackend
from tests.test_memory_qdrant_recall import OfflineEmbedder, save
from tests.test_qdrant_vector_sync import ConditionalREST, Response


class MetadataREST(ConditionalREST):
    def __init__(self, model):
        super().__init__(model)
        self.metadata_reads, self.metadata_hook = 0, None
        self.metadata_change, self.query_error = None, None

    def open(self, request, *, timeout):
        metadata = request.get_method() == "GET" and "/points/" not in request.full_url
        if metadata:
            self.metadata_reads += 1
            if self.metadata_hook:
                self.metadata_hook(self.metadata_reads)
        if request.full_url.endswith("/points/query") and self.query_error:
            self.requests.append((request.get_method(), request.full_url, json.loads(request.data)))
            raise self.query_error
        response = super().open(request, timeout=timeout)
        if metadata and self.metadata_change:
            result = json.loads(response.data)["result"]
            return Response(self.metadata_change(result))
        return response


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "ready.sqlite3"), lexical_backend="bm25")
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="ready checks", idempotency_key=uuid4()).room_id
    model = VectorModel("offline-recall-v1", 2)
    backend = QdrantVersionedVectorBackend(QdrantEndpoint(api_key="synthetic-private-key"), model)
    rest = MetadataREST(model)
    backend._opener = rest
    backend.initialize()
    embedder = OfflineEmbedder()
    index = QdrantMemoryVectorIndex(store.database, embedder, backend.endpoint, backend_factory=lambda model: backend)
    worker = MemoryVectorOutbox(store.database, backend)
    worker.register_target()
    fixture = (store, service, room, backend, embedder, index, worker)
    memory = save(fixture, "gateway 读超时 8 秒")
    worker.run_once(limit=200)
    store.memory.vector_index = index
    rest.requests.clear()
    rest.metadata_reads = 0
    return (*fixture, rest, memory)


def operations(rest):
    return [method for method, *_ in rest.requests]


def test_orchestrated_recall_two_fresh_metadata_gets_and_one_query(setup):
    store, _, room, _, embedder, _, _, rest, memory = setup
    events = []
    rest.metadata_hook = lambda n: events.append((n, len(embedder.calls)))
    assert store.memory.retrieve(room, "后台如何解耦") == (memory,)
    assert events == [(1, 0), (2, 1)]
    assert operations(rest) == ["GET", "GET", "POST"] and rest.metadata_reads == 2
    assert rest.requests[-1][1].endswith("/points/query")


def test_single_read_validates_all_indexes_without_second_fetch(setup):
    backend, rest = setup[3], setup[7]
    backend._require_sync_ready()
    assert operations(rest) == ["GET"]
    assert set(rest.schema) == {*PAYLOAD_SCHEMA, "sync_revision"}


@pytest.mark.parametrize("key", [*PAYLOAD_SCHEMA, "sync_revision"])
@pytest.mark.parametrize("after_embedding", [False, True])
def test_missing_indexes_rejected_at_both_boundaries(setup, key, after_embedding, caplog):
    store, _, room, _, embedder, _, _, rest, memory = setup
    def change(n):
        if n == (2 if after_embedding else 1):
            rest.schema.pop(key)
    rest.metadata_hook = change
    assert store.memory.retrieve(room, "gateway") == (memory,)  # Selected BM25 fallback, not semantic success.
    assert "degraded" in caplog.text and "synthetic-private" not in caplog.text
    assert len(embedder.calls) == int(after_embedding)
    assert operations(rest) == ["GET"] * (2 if after_embedding else 1)


@pytest.mark.parametrize("kind", ["dimensions", "distance", "payload_kind", "sync_kind", "missing_vectors", "schema_shape"])
@pytest.mark.parametrize("after_embedding", [False, True])
def test_changed_metadata_is_not_reused_across_embedding(setup, kind, after_embedding, caplog):
    store, _, room, _, embedder, _, _, rest, memory = setup
    def corrupt(result):
        if kind == "dimensions":
            result["config"]["params"]["vectors"]["size"] = 3
        elif kind == "distance":
            result["config"]["params"]["vectors"]["distance"] = "Dot"
        elif kind == "payload_kind":
            result["payload_schema"]["room_id"] = {"data_type": "integer"}
        elif kind == "sync_kind":
            result["payload_schema"]["sync_revision"] = {"data_type": "keyword"}
        elif kind == "missing_vectors":
            result["config"]["params"].pop("vectors")
        else:
            result["payload_schema"] = []
        return result
    def change(n):
        if n == (2 if after_embedding else 1):
            rest.metadata_change = corrupt
    rest.metadata_hook = change
    assert store.memory.retrieve(room, "gateway") == (memory,)
    assert "degraded" in caplog.text and len(embedder.calls) == int(after_embedding)
    assert operations(rest) == ["GET"] * (2 if after_embedding else 1)


@pytest.mark.parametrize("schema", [None, [], "private-provider-body", True, {}, {"data_type": "keyword"}])
def test_malformed_revision_index_has_safe_typed_error(setup, schema):
    backend, rest = setup[3], setup[7]
    rest.schema["sync_revision"] = schema
    with pytest.raises(VectorBackendError) as caught:
        backend._require_sync_ready()
    assert "private" not in str(caught.value) and operations(rest) == ["GET"]


@pytest.mark.parametrize("after_embedding", [False, True])
def test_collection_removed_causes_no_repair_or_query(setup, after_embedding):
    store, _, room, _, embedder, _, _, rest, memory = setup
    def remove(n):
        if n == (2 if after_embedding else 1):
            rest.exists = False
    rest.metadata_hook = remove
    assert store.memory.retrieve(room, "gateway") == (memory,)
    assert len(embedder.calls) == int(after_embedding)
    assert operations(rest) == ["GET"] * (2 if after_embedding else 1)


def test_next_recall_checks_fresh_state_and_can_recover(setup):
    store, _, room, _, embedder, _, _, rest, memory = setup
    assert store.memory.retrieve(room, "后台如何解耦") == (memory,)
    rest.schema.pop("sync_revision")
    assert store.memory.retrieve(room, "后台如何解耦") == ()
    assert len(embedder.calls) == 1  # Second request fails before the query vector.
    rest.schema["sync_revision"] = {"data_type": "integer"}
    assert store.memory.retrieve(room, "后台如何解耦") == (memory,)
    assert rest.metadata_reads == 5 and len(embedder.calls) == 2


def test_embedding_error_does_not_poison_future_readiness(setup):
    store, _, room, _, embedder, _, _, rest, memory = setup
    embedder.fail = True
    assert store.memory.retrieve(room, "gateway") == (memory,) and operations(rest) == ["GET"]
    embedder.fail = False
    assert store.memory.retrieve(room, "后台如何解耦") == (memory,)
    assert operations(rest) == ["GET", "GET", "GET", "POST"]


@pytest.mark.parametrize("kind", ["connection", "http"])
def test_query_failure_after_valid_readiness_is_not_retried(setup, kind, caplog):
    store, _, room, _, _, _, _, rest, memory = setup
    rest.query_error = (OSError("private-provider-response") if kind == "connection" else
        urllib.error.HTTPError("https://private", 503, "private-key", {}, io.BytesIO(b"private-response")))
    assert store.memory.retrieve(room, "gateway") == (memory,)
    assert operations(rest) == ["GET", "GET", "POST"]
    assert "private" not in caplog.text and "degraded" in caplog.text


def test_standalone_current_search_one_get_one_post_and_scoped_fence(setup):
    store, _, room, backend, _, _, worker, rest, memory = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                           (str(memory.memory_id),)).fetchone()
        mutation = worker.desired_in_snapshot(conn, row)[0]
    request = VectorSearch(room, backend.model, (1., 0.), (mutation.record.anchor,))
    assert backend.search_current(request, (mutation,))[0].anchor.memory_id == memory.memory_id
    assert operations(rest) == ["GET", "POST"]
    filters = rest.requests[-1][2]["filter"]["should"][0]["must"]
    assert {"key": "sync_revision", "match": {"value": mutation.revision}} in filters
    assert {"key": "sync_vector_hash", "match": {"value": mutation.vector_hash}} in filters


def test_versioned_noncurrent_search_also_requires_revision_index(setup):
    store, _, room, backend, _, _, worker, rest, memory = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                           (str(memory.memory_id),)).fetchone()
        mutation = worker.desired_in_snapshot(conn, row)[0]
    rest.schema.pop("sync_revision")
    with pytest.raises(VectorBackendError):
        backend.search(VectorSearch(room, backend.model, (1., 0.), (mutation.record.anchor,)))
    assert operations(rest) == ["GET"]


@pytest.mark.parametrize("kind", ["empty", "zero", "model", "mutations"])
def test_invalid_or_empty_current_query_does_not_check_metadata(setup, kind):
    store, _, room, backend, _, _, worker, rest, memory = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                           (str(memory.memory_id),)).fetchone()
        mutation = worker.desired_in_snapshot(conn, row)[0]
    request = VectorSearch(room, backend.model, (1., 0.), (mutation.record.anchor,))
    items = (mutation,)
    if kind == "empty":
        assert backend.search_current(replace(request, candidates=()), ()) == ()
    else:
        if kind == "zero":
            request = replace(request, vector=(0., 0.))
        elif kind == "model":
            request = replace(request, model=VectorModel("other-model", 2))
        else:
            items = ()
        with pytest.raises(ValueError):
            backend.search_current(request, items)
    assert not rest.requests


def test_plain_backend_search_does_not_require_versioned_index():
    model = VectorModel("plain-v1", 2)
    backend = QdrantMemoryVectorBackend(QdrantEndpoint(), model)
    rest = MetadataREST(model)
    backend._opener = rest
    backend.initialize()
    rest.requests.clear()
    from tests.test_qdrant_vector_backend import anchor
    item = anchor()
    assert backend.search(VectorSearch(item.room_id, model, (1., 0.), (item,))) == ()
    assert "sync_revision" not in rest.schema and operations(rest) == ["GET", "POST"]


def test_repeated_read_control_has_same_hits_filters_and_original_http_shape(setup):
    store, _, room, backend, _, _, worker, rest, memory = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                           (str(memory.memory_id),)).fetchone()
        mutation = worker.desired_in_snapshot(conn, row)[0]
    request = VectorSearch(room, backend.model, (1., 0.), (mutation.record.anchor,))
    backend._require_sync_ready()  # Orchestrator's first gate.
    actual = backend.search_current(request, (mutation,))
    actual_query = rest.requests[-1][2]
    assert operations(rest) == ["GET", "GET", "POST"]
    reference = RepeatedReadinessBackend(backend.endpoint, backend.model)
    reference._opener = rest
    assert reference.sync_target_id == backend.sync_target_id
    rest.requests.clear()
    reference._require_sync_ready()
    expected = reference.search_current(request, (mutation,))
    assert actual == expected and rest.requests[-1][2] == actual_query
    assert operations(rest) == ["GET"] * 5 + ["POST"]
