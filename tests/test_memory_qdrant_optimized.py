"""Extended regression validity; fake remote/control is not live fault evidence."""

import copy
import json
import urllib.request
from types import SimpleNamespace

import pytest

from app.chat.qdrant_vector_backend import PAYLOAD_SCHEMA, QdrantEndpoint, QdrantMemoryVectorBackend
from app.chat.vector_contracts import VectorBackendError, VectorHit
from app.chat.vector_memory import _cosine
from evals import check_memory_qdrant_optimized as probe
from tests.test_memory_qdrant_faults import OfflineStorage


class GuardStorage(OfflineStorage):
    _path = "/collections/synthetic_optimized"
    _completed = staticmethod(QdrantMemoryVectorBackend._completed)

    def __init__(self):
        super().__init__()
        self.initialize()

    def initialize(self):
        self.schema = {**{k: {"data_type": v} for k, v in PAYLOAD_SCHEMA.items()},
                       "sync_revision": {"data_type": "integer"}}

    def _request(self, method, path, *args, **kwargs):
        if not self.running:
            raise VectorBackendError("synthetic stopped")
        if method == "DELETE" and "/index/" in path:
            self.schema.pop(path.split("/index/", 1)[1].split("?", 1)[0])
            return {"status": "completed"}
        return copy.deepcopy(self.schema) if method == "GET" else None

    def _require_sync_ready(self):
        schema = self._request("GET", self._path)
        if {*PAYLOAD_SCHEMA, "sync_revision"} - set(schema):
            raise VectorBackendError("synthetic missing index")

    def search_current(self, request, mutations):
        self._require_sync_ready()
        hits = tuple(sorted((VectorHit(m.record.anchor, request.model, _cosine(request.vector, m.record.vector))
            for m in mutations if self.points.get(self.point_id(m)) == m and m.record is not None
            and _cosine(request.vector, m.record.vector) >= request.min_similarity),
            key=lambda h: (-h.similarity, str(h.anchor.memory_id))))
        self._request("POST", self._path + "/points/query")  # Controlled hook after captured fake response.
        return hits


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(probe, "CountedDocker", lambda *a, **k: pytest.fail("Docker"))


@pytest.mark.parametrize("mode", probe.MODES)
def test_full_extended_journey_real_sqlite_threads_fake_remote(tmp_path, mode):
    storage = GuardStorage()
    journey = probe.OptimizedJourney(tmp_path / "isolated.sqlite3", storage, QdrantEndpoint(), storage,
        lexical_backend=mode, progress=lambda *a, **k: None)
    report = journey.run()
    assert len(report["checks"]) == 23 and len(report["read_boundaries"]) == 13
    assert storage.stops == storage.starts == 7
    assert report["final_audit"]["consistent_at_observation"]
    assert not any(report["outbox_status"].get(k) for k in ("pending", "processing", "waiting", "dead"))
    assert report["read_boundaries"][0]["snapshot_selects"] == [5, 5]
    assert "independent Python thread" in report["race_scope"]
    assert "no OS kill" in report["worker_crash_scope"]


@pytest.mark.parametrize("failure", ["request", "snapshot"])
def test_observers_restore_instance_methods_when_call_fails(tmp_path, failure):
    storage = GuardStorage()
    journey = probe.OptimizedJourney(tmp_path / "isolated.sqlite3", storage, QdrantEndpoint(), storage)
    before_request, before_snapshot = dict(vars(storage)), dict(vars(journey.index))
    with pytest.raises(RuntimeError), journey.observe():
        if failure == "request":
            storage._request("PUT", storage._path)
        raise RuntimeError("private-error")
    assert vars(storage) == before_request and vars(journey.index) == before_snapshot


def test_concurrent_commit_failure_is_safe_category():
    def fail():
        raise RuntimeError("private-key-and-response")
    with pytest.raises(RuntimeError, match="concurrent_writer_failed") as caught:
        probe.concurrent_commit(fail)
    assert "private" not in str(caught.value)


def test_concurrent_join_is_bounded_and_timeout_never_passes(monkeypatch):
    class Thread:
        def __init__(self, **kwargs):
            assert kwargs["daemon"] is True
        def start(self):
            pass
        def join(self, *, timeout):
            assert timeout == 5
        def is_alive(self):
            return True
    monkeypatch.setattr(probe.threading, "Thread", Thread)
    with pytest.raises(RuntimeError, match="concurrent_writer_timeout"):
        probe.concurrent_commit(lambda: None)


def test_matrix_detects_disabled_post_query_cache_revalidation(tmp_path, monkeypatch):
    storage = GuardStorage()
    journey = probe.OptimizedJourney(tmp_path / "isolated.sqlite3", storage, QdrantEndpoint(), storage,
                                    progress=lambda *a, **k: None)
    monkeypatch.setattr(probe, "RACES", ("cache",))
    snapshot = journey.index._snapshot
    saved = []
    def stale(conn, *a, **k):
        result = snapshot(conn, *a, **k)
        saved.append(result)
        return saved[0]  # Deliberately discard post-query fresh data.
    journey.index._snapshot = stale
    with pytest.raises(RuntimeError, match="post_query_stale_hit_leaked"):
        journey.races()


def test_matrix_detects_skipped_schema_check_not_just_same_lexical_answer(tmp_path):
    storage = GuardStorage()
    journey = probe.OptimizedJourney(tmp_path / "isolated.sqlite3", storage, QdrantEndpoint(), storage,
                                    progress=lambda *a, **k: None)
    def ignore_schema():
        storage._request("GET", storage._path)
    storage._require_sync_ready = ignore_schema
    with pytest.raises(RuntimeError, match="schema_fault_not_at_expected_gate"):
        journey.schema_boundaries()


def test_default_plan_never_runs_probe(monkeypatch, capsys):
    monkeypatch.setattr(probe, "probe", lambda **k: pytest.fail("plan ran"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


def test_incomplete_probe_is_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "probe", lambda: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as caught:
        probe.main(["--live"])
    assert caught.value.code == 1


@pytest.mark.parametrize("failure", ["create", "start", "initialize", "legacy", "bm25", "cleanup", None])
def test_safe_partial_checkpoint_and_cleanup(tmp_path, monkeypatch, failure):
    calls = []
    def trigger(stage):
        calls.append(stage)
        if failure == stage:
            raise RuntimeError("private-key-and-response")
    class Control:
        url, image_id, name = "http://127.0.0.1:19101", "sha256:synthetic", "synthetic-owned"
        stops, starts = 14, 15
        def __init__(self, *a):
            pass
        def create(self):
            trigger("create")
        def start(self):
            trigger("start")
        def cleanup(self):
            trigger("cleanup")
            return True
    class Backend:
        def __init__(self, *a):
            pass
        def initialize(self):
            trigger("initialize")
    class Journey:
        def __init__(self, *a, lexical_backend):
            self.mode, self.checks, self.boundaries = lexical_backend, [], []
        def run(self):
            trigger(self.mode)
            return {"checks": ["synthetic"]}
    monkeypatch.setattr(probe, "CountedDocker", Control)
    monkeypatch.setattr(probe, "QdrantVersionedVectorBackend", Backend)
    monkeypatch.setattr(probe, "OptimizedJourney", Journey)
    output, report = probe.probe(output_root=tmp_path)
    assert calls[-1] == "cleanup" and report["complete"] is (failure is None)
    assert report["cleanup_complete"] is (failure != "cleanup")
    assert report["embedding_calls"] == report["agent_calls"] == 0
    assert not report["default_changed"]
    text = (output / "report.json").read_text()
    assert "private" not in text
    assert json.loads(text)["complete"] == report["complete"]
    if failure == "bm25":
        assert tuple(report["modes"]) == ("legacy",)


def test_incomplete_stop_budget_does_not_pass(tmp_path, monkeypatch):
    class Control:
        url, image_id, name = "http://127.0.0.1:19101", "synthetic", "synthetic"
        stops, starts = 0, 1
        def __init__(self, *a):
            pass
        def create(self):
            pass
        def start(self):
            pass
        def cleanup(self):
            return True
    monkeypatch.setattr(probe, "CountedDocker", Control)
    monkeypatch.setattr(probe, "QdrantVersionedVectorBackend", lambda *a: SimpleNamespace(initialize=lambda: None))
    monkeypatch.setattr(probe, "OptimizedJourney", lambda *a, **k: SimpleNamespace(run=dict, checks=[], boundaries=[]))
    _, report = probe.probe(output_root=tmp_path)
    assert not report["complete"] and report["cleanup_complete"]
    assert report["error_type"] == "RuntimeError"
