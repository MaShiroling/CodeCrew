"""Instrumentation validity only; fake HTTP timings are not performance evidence."""

import json
from types import SimpleNamespace

import pytest

from evals import benchmark_memory_qdrant as bench
from evals import profile_memory_qdrant as profile
from tests.test_memory_qdrant_benchmark import Remote, embeddings
from tests.test_memory_retrieval_ab import _case


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(bench, "OwnedDocker", lambda *a, **k: pytest.fail("Docker"))


class HttpShapeRemote(Remote):
    _path = "/collections/diagnostic-fixture"
    def _request(self, *args, **kwargs):
        return {}  # Only method/endpoint shape is faked; no real Qdrant latency claim.
    def _require_sync_ready(self):
        super()._require_sync_ready()
        self._request("GET", self._path)
    def search_current(self, request, mutations):
        hits = super().search_current(request, mutations)
        self._request("POST", self._path + "/points/query")
        return hits


def quality(tmp_path, *, case=None, remote=None, repeats=2):
    cache = embeddings()
    remote = remote or HttpShapeRemote(bench.VectorModel(cache.model_fingerprint, 3))
    rows, summary = bench.quality_suite([case or _case()], tmp_path / "quality.sqlite3", cache, remote,
        bench.QdrantEndpoint(), repeats=repeats, warmups=1, progress=lambda *a, **k: None,
        read_runner=profile.profiled_reads)
    return rows, summary, cache


def test_nested_exclusive_spans_partition_root_including_repeated_children():
    ticks = iter((0, 10, 20, 30, 40, 50))
    spans = profile.Spans(clock=lambda: next(ticks))
    with spans.span("retrieve_other"):
        with spans.span("http_metadata"):
            pass
        with spans.span("http_metadata"):
            pass
    assert spans.inclusive == {"retrieve_other": 50, "http_metadata": 20}
    assert spans.exclusive == {"retrieve_other": 30, "http_metadata": 20}
    assert sum(spans.exclusive.values()) == spans.inclusive["retrieve_other"]
    assert spans.calls["http_metadata"] == 2 and not spans.stack


def test_exception_unwinds_nested_timer():
    ticks = iter((0, 2, 3, 5))
    spans = profile.Spans(clock=lambda: next(ticks))
    with pytest.raises(RuntimeError), spans.span("retrieve_other"), spans.span("http_query"):
        raise RuntimeError("not retained")
    assert not spans.stack and sum(spans.exclusive.values()) == 5


def test_unknown_span_rejected():
    with pytest.raises(ValueError), profile.Spans().span("raw-private-url"):
        pass


def test_real_sqlite_four_arms_parity_additivity_http_sql_and_no_model(tmp_path):
    rows, summary, cache = quality(tmp_path)
    assert tuple(rows[0]["arms"]) == profile.ARMS
    assert summary["paired_storage"]["ordered_sources_changed"] == []
    assert cache.calls == cache.errors == 0
    arm = rows[0]["arms"]["qdrant_profiled"]
    assert arm["sources"] == ["m1"] and len(arm["profiles"]) == 2
    for sample in arm["profiles"]:
        assert sum(sample["exclusive_ms"].values()) == pytest.approx(sample["root_ms"])
        assert sample["calls"]["http_metadata"] == 2 and sample["calls"]["http_query"] == 1
        assert sample["calls"]["cached_query_vector"] == 1
        assert sample["visible_sql"]["canonical_pre_snapshot"]["SELECT"] >= 2
        assert sample["visible_sql"]["canonical_post_snapshot"]["SELECT"] >= 2
        serialized = json.dumps(sample)
        assert "RabbitMQ" not in serialized and "SELECT *" not in serialized
        assert "/collections" not in serialized and "diagnostic-fixture" not in serialized
    report = profile.profile_summary(rows)
    assert report["ordered_sources_equal"] and report["samples"] == 2
    assert sum(p["exclusive_share"] for p in report["phases"].values()) == pytest.approx(1)
    assert report["profiled_to_unprofiled_mean_ratio"] > 0


@pytest.mark.parametrize("room,discussion", [("other", "d3"), ("project", "d1")])
def test_empty_candidates_have_no_http_or_query_vectors(tmp_path, room, discussion):
    case = _case(query_room=room, query_discussion=discussion)
    if room == "project":
        case["messages"][1]["discussion"] = discussion  # Both facts excluded, not just the answer fact.
    rows, _, cache = quality(tmp_path, case=case)
    for sample in rows[0]["arms"]["qdrant_profiled"]["profiles"]:
        assert not sample["calls"].get("http_metadata")
        assert not sample["calls"].get("http_query")
        assert not sample["calls"].get("cached_query_vector")
    assert cache.calls == cache.errors == 0


def test_synthetic_scale_preserves_scope_and_counts(tmp_path):
    remote = HttpShapeRemote(bench.VectorModel(bench.SyntheticVectors.model_fingerprint, 1024))
    result = bench.scale_suite((3, 7), tmp_path / "scale.sqlite3", remote, bench.QdrantEndpoint(),
        repeats=1, warmups=0, read_runner=profile.profiled_reads)
    assert result["initial_audit"]["consistent_at_observation"]
    assert profile.profile_summary(result["rows"])["ordered_sources_equal"]
    for row in result["rows"]:
        sample = row["arms"]["qdrant_profiled"]["profiles"][0]
        assert sample["visible_sql"]["canonical_pre_snapshot"]["SELECT"] == 5
        assert sample["visible_sql"]["canonical_post_snapshot"]["SELECT"] == 5


@pytest.mark.parametrize("fail", [False, True])
def test_instance_attributes_restored_exactly_after_exit_and_exception(tmp_path, fail):
    cache = embeddings()
    remote = HttpShapeRemote(bench.VectorModel(cache.model_fingerprint, 3))
    store, _, _, _ = bench._seed_case(_case(), tmp_path / "restore.sqlite3")
    index = bench.prepared_indexes(store, cache, remote, bench.QdrantEndpoint())["qdrant_hybrid"].inner
    # Preserve an existing instance override as well as class-bound methods.
    remote._request = lambda *a, **k: {}
    objects = [store.memory, store.memory.fts_index, store.database, index, cache, remote]
    before = [dict(vars(obj)) for obj in objects]
    capture = profile.Capture(store, index, remote)
    try:
        with capture:
            assert "retrieve" in vars(store.memory)
            if fail:
                raise RuntimeError("private payload")
    except RuntimeError:
        assert fail
    assert [dict(vars(obj)) for obj in objects] == before
    assert not capture.restores


def test_partial_install_failure_restores_earlier_patches(tmp_path, monkeypatch):
    cache = embeddings()
    remote = HttpShapeRemote(bench.VectorModel(cache.model_fingerprint, 3))
    store, _, _, _ = bench._seed_case(_case(), tmp_path / "partial.sqlite3")
    index = bench.prepared_indexes(store, cache, remote, bench.QdrantEndpoint())["qdrant_hybrid"].inner
    before = dict(vars(store.memory))
    monkeypatch.setattr(store.memory, "fts_index", SimpleNamespace())
    with pytest.raises(AttributeError), profile.Capture(store, index, remote):
        pass
    assert "retrieve" not in vars(store.memory)
    assert store.memory.vector_index == before["vector_index"]


def test_unexpected_write_endpoint_blocked_before_delegate(tmp_path):
    cache = embeddings()
    remote = HttpShapeRemote(bench.VectorModel(cache.model_fingerprint, 3))
    store, _, _, _ = bench._seed_case(_case(), tmp_path / "guard.sqlite3")
    index = bench.prepared_indexes(store, cache, remote, bench.QdrantEndpoint())["qdrant_hybrid"].inner
    calls = []
    remote._request = lambda *a, **k: calls.append(a)
    with profile.Capture(store, index, remote) as capture:
        capture.active = True
        with pytest.raises(RuntimeError, match="unexpected diagnostic"):
            remote._request("PUT", remote._path, {"secret": "private"})
        capture.active = False
    assert calls == []


def test_swallowed_remote_error_is_invalid_experiment_not_scored(tmp_path):
    remote = HttpShapeRemote(bench.VectorModel("a" * 64, 3))
    remote.search_current = lambda *a: (_ for _ in ()).throw(RuntimeError("private response"))
    with pytest.raises(RuntimeError, match="private response"):
        quality(tmp_path, remote=remote)


def test_instrumentation_drift_rejected(tmp_path, monkeypatch):
    original = profile.Capture.__enter__
    def drift(self):
        result = original(self)
        retrieve = self.store.memory.retrieve
        def discard(*a, **k):
            retrieve(*a, **k)
            return ()
        self.patch(self.store.memory, "retrieve", discard)
        return result
    monkeypatch.setattr(profile.Capture, "__enter__", drift)
    with pytest.raises(RuntimeError, match="instrumentation_changed_sources"):
        quality(tmp_path)


@pytest.mark.parametrize("repeats,warmups,scales", [(0, 0, ((1, 1),)), (True, 0, ((1, 1),)),
    (1, -1, ((1, 1),)), (1, 0, ((201, 201),)), (1, 0, ())])
def test_bounds_before_cache_or_docker(tmp_path, repeats, warmups, scales):
    with pytest.raises(ValueError):
        profile.run_suite(cache=tmp_path / "absent.json", repeats=repeats, warmups=warmups, scales=scales)


def test_plan_has_no_side_effects(monkeypatch, capsys):
    monkeypatch.setattr(profile, "run_suite", lambda **k: pytest.fail("plan ran"))
    profile.main([])
    assert "No settings/cloud/Agent" in capsys.readouterr().out


def test_live_requires_explicit_cache():
    with pytest.raises(SystemExit) as exc:
        profile.main(["--live"])
    assert exc.value.code == 2


def test_empty_summary_has_no_fabricated_ratios():
    result = profile.profile_summary([])
    assert result["samples"] == 0 and result["profiled_to_unprofiled_mean_ratio"] is None


@pytest.mark.parametrize("failure", ["create", "start", "initialize", "quality", "scale", "cleanup", None])
def test_safe_checkpoint_cleanup_and_no_partial_success(tmp_path, monkeypatch, failure):
    calls = []
    def trigger(stage):
        calls.append(stage)
        if failure == stage:
            raise RuntimeError("private-key-and-response")
    class Control:
        name, image_id, url = "synthetic-owned", "sha256:synthetic", "http://127.0.0.1:19001"
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
    def quality_stub(*a, **k):
        assert k["read_runner"] is profile.profiled_reads
        trigger("quality")
        return [], {}
    def scale_stub(*a, **k):
        assert k["read_runner"] is profile.profiled_reads
        trigger("scale")
        return {"rows": [], "summary": {}}
    cache = tmp_path / "cache.json"
    cache.write_text("synthetic")
    monkeypatch.setattr(bench, "OwnedDocker", Control)
    monkeypatch.setattr(bench, "QdrantVersionedVectorBackend", Backend)
    monkeypatch.setattr(bench, "validate_unseen", lambda: [_case()])
    monkeypatch.setattr(bench, "verify_frozen", lambda *a: None)
    monkeypatch.setattr(bench.SharedEmbeddings, "load", lambda *a: (embeddings(), {"model": "synthetic"}))
    monkeypatch.setattr(bench, "quality_suite", quality_stub)
    monkeypatch.setattr(bench, "scale_suite", scale_stub)
    output, manifest = profile.run_suite(cache=cache, scales=((1, 1),), output_root=tmp_path)
    assert calls[-1] == "cleanup" and manifest["complete"] is (failure is None)
    assert manifest["cleanup_complete"] is (failure != "cleanup")
    assert manifest["embedding_calls"] == manifest["agent_calls"] == 0
    assert manifest["optimization_applied"] and not manifest["default_changed"]
    assert manifest["read_path_revision"] == "codecrew-canonical-batch-v1"
    assert manifest["remote_readiness_revision"] == "codecrew-qdrant-readiness-v2"
    saved = (output / "manifest.json").read_text()
    assert "private-key" not in saved
    assert json.loads(saved)["complete"] == manifest["complete"]


def test_failed_manifest_is_nonzero_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "run_suite", lambda **k: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as exc:
        profile.main(["--live", "--cache", str(tmp_path / "cache.json")])
    assert exc.value.code == 1
