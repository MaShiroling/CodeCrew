"""REST protocol/filters offline; no claim that a fake server is real Qdrant."""

import io
import json
import urllib.error
from dataclasses import replace
from uuid import uuid4

import pytest

from app.chat.qdrant_vector_backend import (
    PAYLOAD_SCHEMA,
    QdrantEndpoint,
    QdrantMemoryVectorBackend,
    _NoRedirect,
)
from app.chat.vector_backend_factory import build_memory_vector_backend
from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorModel,
    VectorRecord,
    VectorSearch,
)
from app.config import Settings
from app.storage import SQLiteDatabase
from evals import check_memory_qdrant as probe_module


def anchor(room=None):
    return VectorAnchor(uuid4(), room or uuid4(), uuid4(), uuid4(), "a" * 64)


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self, maximum):
        return self.data[:maximum]


class FakeREST:
    def __init__(self, model):
        self.model = model
        self.exists = False
        self.schema = {}
        self.requests = []
        self.points = []
        self.fail = None
    def open(self, request, *, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), request.full_url, body, timeout, request))
        if self.fail:
            raise self.fail
        method, url = request.get_method(), request.full_url
        if method == "GET":
            if not self.exists:
                raise urllib.error.HTTPError(url, 404, "missing", {}, io.BytesIO(b"private"))
            result = {"config": {"params": {"vectors": {"size": self.model.dimensions, "distance": "Cosine"}}},
                      "payload_schema": self.schema}
        elif "/index?" in url:
            self.schema[body["field_name"]] = {"data_type": body["field_schema"]}
            result = {"status": "completed"}
        elif url.endswith("/points/query"):
            result = {"points": self.points}
        elif "/points" in url:
            result = {"status": "completed"}
        else:
            self.exists = True
            result = True
        return Response(json.dumps({"status": "ok", "result": result}).encode())


@pytest.fixture
def setup():
    model = VectorModel("model-v1", 2)
    backend = QdrantMemoryVectorBackend(QdrantEndpoint(api_key="test-private-key"), model)
    rest = FakeREST(model)
    backend._opener = rest
    return backend, rest, model


def test_explicit_collection_initialization_is_idempotent(setup):
    backend, rest, _ = setup
    assert rest.requests == []
    backend.initialize()
    assert set(rest.schema) == set(PAYLOAD_SCHEMA)
    writes = sum(method == "PUT" for method, *_ in rest.requests)
    assert writes == 9
    backend.initialize()
    assert sum(method == "PUT" for method, *_ in rest.requests) == writes
    assert rest.requests[0][-1].get_header("Api-key") == "test-private-key"
    assert "test-private-key" not in repr(backend.endpoint)
    assert all(timeout == 5 for _, _, _, timeout, _ in rest.requests)


def test_upsert_has_only_metadata_and_scoped_id_and_wait(setup):
    backend, rest, model = setup
    backend.initialize()
    item = anchor()
    assert backend.upsert((VectorRecord(item, model, (1.0, 0.0)),)) == (item.memory_id,)
    assert backend.upsert((VectorRecord(item, model, (1.0, 0.0)),)) == (item.memory_id,)
    method, url, body, *_ = rest.requests[-1]
    assert method == "PUT" and url.endswith("/points?wait=true")
    assert set(body["points"][0]["payload"]) == set(PAYLOAD_SCHEMA)
    assert body["points"][0]["id"] == backend.point_id(item)
    assert backend.point_id(replace(item, room_id=uuid4())) != backend.point_id(item)
    assert "content" not in body["points"][0]["payload"]


def test_search_filters_all_anchor_fields_before_limit_and_sorts_ties(setup):
    backend, rest, model = setup
    backend.initialize()
    a = anchor()
    b = anchor(a.room_id)
    rest.points = [{"id": backend.point_id(item), "payload": backend._payload(item), "score": 0.9}
                   for item in (b, a)]
    req = VectorSearch(a.room_id, model, (1.0, 0.0), (a, b), exclude_correlation_id=uuid4(), limit=1)
    hits = backend.search(req)
    body = rest.requests[-1][2]
    assert body["limit"] == 2  # obtain all ties; apply caller's limit only after deterministic sorting
    assert body["params"] == {"exact": True}
    assert body["score_threshold"] == 0.65
    assert body["with_payload"] and not body["with_vector"]
    assert body["filter"]["must_not"][0]["key"] == "source_correlation_id"
    assert {condition["key"] for condition in body["filter"]["must"]} == {
        "room_id", "status", "model_fingerprint", "dimensions"}
    for item, clause in zip((a, b), body["filter"]["should"], strict=True):
        assert clause["must"][0] == {"has_id": [backend.point_id(item)]}
        assert {c["key"]: c["match"]["value"] for c in clause["must"][1:]} == backend._payload(item)
    assert hits[0].anchor.memory_id == min((a.memory_id, b.memory_id), key=str)


@pytest.mark.parametrize("bad", ["id", "duplicate", "payload", "model", "score_nan", "score_bool", "score_range", "oversized"])
def test_invalid_server_points_fail_closed(setup, bad):
    backend, rest, model = setup
    backend.initialize()
    a, b = anchor(), None
    b = anchor(a.room_id)
    point = {"id": backend.point_id(a), "payload": backend._payload(a), "score": 0.9}
    rest.points = [point]
    if bad == "id":
        point["id"] = str(uuid4())
    elif bad == "duplicate":
        rest.points.append(point)
    elif bad == "payload":
        point["payload"]["content_hash"] = "b" * 64
    elif bad == "model":
        point["payload"]["model_fingerprint"] = "wrong-model"
    elif bad == "score_nan":
        point["score"] = float("nan")
    elif bad == "score_bool":
        point["score"] = True
    elif bad == "score_range":
        point["score"] = 7
    else:
        rest.points = [point] * 3
    with pytest.raises(VectorBackendError):
        backend.search(VectorSearch(a.room_id, model, (1.0, 0.0), (a, b)))


def test_delete_scoped_to_room_and_model(setup):
    backend, rest, _ = setup
    backend.initialize()
    a = anchor()
    backend.delete(a.room_id, (a.memory_id,))
    method, url, body, *_ = rest.requests[-1]
    assert method == "POST" and url.endswith("/points/delete?wait=true")
    assert body["filter"]["must"] == [
        {"key": "room_id", "match": {"value": str(a.room_id)}},
        {"key": "model_fingerprint", "match": {"value": "model-v1"}},
        {"key": "memory_id", "match": {"any": [str(a.memory_id)]}},
    ]


def test_missing_collection_is_not_silent_empty_or_auto_creation(setup):
    backend, rest, model = setup
    a = anchor()
    for operation in (lambda: backend.upsert((VectorRecord(a, model, (1.0, 0.0)),)),
                      lambda: backend.delete(a.room_id, (a.memory_id,)),
                      lambda: backend.search(VectorSearch(a.room_id, model, (1.0, 0.0), (a,)))):
        with pytest.raises(VectorBackendError, match="not initialized"):
            operation()
    assert all(method == "GET" for method, *_ in rest.requests)


def test_empty_operations_are_no_network_success(setup):
    backend, rest, model = setup
    assert backend.upsert(()) == ()
    backend.delete(uuid4(), ())
    assert backend.search(VectorSearch(uuid4(), model, (1.0, 0.0), ())) == ()
    assert rest.requests == []


def test_connection_and_http_errors_are_safe_and_not_retried(setup):
    backend, rest, _ = setup
    for error in (OSError("private-key provider-url"),
                  urllib.error.HTTPError("https://private", 401, "private-key", {}, io.BytesIO(b"secret"))):
        rest.fail = error
        calls = len(rest.requests)
        with pytest.raises(VectorBackendError) as caught:
            backend.initialize()
        assert "private" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert len(rest.requests) == calls + 1


@pytest.mark.parametrize("body", [b"not json", b"{}", b'"not envelope"', b'{"status":"error","result":"secret"}', b"x" * 2_000_001])
def test_invalid_envelope_is_safe(setup, body):
    backend, _, _ = setup
    class Raw:
        def open(self, *args, **kwargs):
            return Response(body)
    backend._opener = Raw()
    with pytest.raises(VectorBackendError, match="invalid Qdrant response"):
        backend.initialize()


def test_incompatible_collection_never_recreated(setup):
    backend, rest, _ = setup
    rest.exists = True
    rest.model = VectorModel("model-v1", 3)
    with pytest.raises(VectorBackendError, match="incompatible"):
        backend.initialize()
    assert len(rest.requests) == 1
    assert rest.requests[0][0] == "GET"


def test_incompatible_payload_index_never_overwritten(setup):
    backend, rest, _ = setup
    rest.exists = True
    rest.schema = {"room_id": {"data_type": "integer"}}
    with pytest.raises(VectorBackendError, match="incompatible"):
        backend.initialize()
    assert len(rest.requests) == 1


def test_pending_writes_not_claimed_as_success(setup):
    backend, rest, model = setup
    backend.initialize()
    original = rest.open
    def pending(request, **kwargs):
        if request.full_url.endswith("/points?wait=true"):
            return Response(b'{"status":"ok","result":{"status":"acknowledged"}}')
        return original(request, **kwargs)
    rest.open = pending
    with pytest.raises(VectorBackendError, match="not completed"):
        backend.upsert((VectorRecord(anchor(), model, (1.0, 0.0)),))


def test_zero_norm_and_model_mismatch_fail_before_network(setup):
    backend, rest, model = setup
    a = anchor()
    for operation in (lambda: backend.upsert((VectorRecord(a, model, (0.0, 0.0)),)),
                      lambda: backend.search(VectorSearch(a.room_id, model, (0.0, 0.0), (a,))),
                      lambda: backend.search(VectorSearch(a.room_id, VectorModel("other", 2), (1.0, 0.0), (a,)))):
        with pytest.raises(ValueError):
            operation()
    assert rest.requests == []


@pytest.mark.parametrize("url", ["http://example.test:6333", "http://127.0.0.1:6333/path",
                                  "https://name:secret@example.test", "https://example.test?q=secret",
                                  "https://example.test#secret", "http://127.0.0.1:0", "not-url"])
def test_endpoint_rejects_unsafe_configuration(url):
    with pytest.raises(ValueError):
        QdrantEndpoint(base_url=url)


def test_redirects_are_blocked():
    assert _NoRedirect().redirect_request(None, None, 302, None, None, "https://elsewhere") is None


def test_factory_default_and_opt_in_do_not_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(QdrantMemoryVectorBackend, "_request", lambda *a, **k: pytest.fail("factory connected"))
    database = SQLiteDatabase(tmp_path / "unused.sqlite3")
    model = VectorModel("model-v1", 2)
    defaults = Settings(_env_file=None)
    assert defaults.memory_vector_store == "sqlite"
    assert type(build_memory_vector_backend(defaults, database, model)).__name__ == "SQLiteMemoryVectorBackend"
    selected = Settings(_env_file=None, memory_vector_store="qdrant", memory_qdrant_api_key="secret")
    assert isinstance(build_memory_vector_backend(selected, database, model), QdrantMemoryVectorBackend)
    assert "secret" not in repr(selected)
    assert not database.path.exists()


def test_probe_default_does_not_read_settings_or_network(monkeypatch, capsys):
    monkeypatch.setattr(probe_module, "Settings", lambda: pytest.fail("settings opened"))
    monkeypatch.setattr("sys.argv", ["check_memory_qdrant"])
    probe_module.main()
    assert "Plan only" in capsys.readouterr().out


def test_failed_probe_has_safe_incomplete_artifact_and_no_cleanup_of_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_module, "Settings", lambda: Settings(_env_file=None))
    requests = []
    def fail(self, method, path, *args, **kwargs):
        requests.append(method)
        raise VectorBackendError("safe failure")
    monkeypatch.setattr(QdrantMemoryVectorBackend, "_request", fail)
    path, report = probe_module.probe(output_root=tmp_path)
    assert not report["complete"] and not report["cleanup_complete"]
    assert report["error_type"] == "VectorBackendError"
    assert requests == ["GET"]  # no initialization attempt; no DELETE
    assert (path / "report.json").exists()


def test_probe_never_adopts_or_deletes_a_collection_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_module, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(QdrantMemoryVectorBackend, "_collection", lambda self: PAYLOAD_SCHEMA)
    monkeypatch.setattr(QdrantMemoryVectorBackend, "_request", lambda *a, **k: pytest.fail("probe touched existing collection"))
    _, report = probe_module.probe(output_root=tmp_path)
    assert not report["complete"]
    assert report["error_type"] == "RuntimeError"
