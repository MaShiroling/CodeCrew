"""Experiment validity; fake storage/vectors are not quality/performance claims."""

import json
import sqlite3
from dataclasses import replace

import pytest

from app.chat.fts_memory import MemoryFTSIndex
from app.chat.memory import ChatMemoryStore
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.vector_memory import MemoryVectorIndex
from evals import benchmark_memory_qdrant as bench
from evals.run_memory_fts_ab import SharedEmbeddings
from tests.test_memory_qdrant_faults import OfflineStorage
from tests.test_memory_retrieval_ab import _case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected provider"))
    monkeypatch.setattr(bench, "OwnedDocker", lambda *a, **k: pytest.fail("offline test used Docker"))


def embeddings(case=None):
    cache = SharedEmbeddings(fingerprint="a" * 64)
    for text, vector in (("后台任务队列决定用 RabbitMQ。", (1., 0., 0.)),
                         ("审计日志决定按天归档。", (0., 1., 0.)),
                         ("异步作业要接哪种消息中间件？", (1., 0., 0.)),
                         ("RabbitMQ", (1., 0., 0.))):
        cache.vectors[cache.key(text)] = cache._validate(vector)
    return cache


class Remote(OfflineStorage):
    def __init__(self, model):
        super().__init__()
        self.model = model
    def _request(self, *args, **kwargs):
        raise AssertionError("fake remote has no REST")


def quality(tmp_path, *, case=None, cache=None, remote=None, repeats=2, warmups=1):
    cache = cache or embeddings()
    remote = remote or Remote(bench.VectorModel(cache.model_fingerprint, 3))
    return bench.quality_suite([case or _case()], tmp_path / "quality.sqlite3", cache, remote, QdrantEndpoint(),
                               repeats=repeats, warmups=warmups, progress=lambda *a, **k: None)


def test_paired_quality_controls_cache_snapshot_and_sample_counts(tmp_path):
    cache = embeddings()
    rows, summary = quality(tmp_path, cache=cache)
    row = rows[0]
    assert tuple(row["arms"]) == bench.ARMS
    assert row["arms"]["sqlite_hybrid"]["sources"] == row["arms"]["qdrant_hybrid"]["sources"] == ["m1"]
    assert row["arms"]["bm25"]["sources"] == []
    assert all(len(arm["samples_ms"]) == 2 for arm in row["arms"].values())
    assert row["storage_parity"]["ordered_ids_equal"]
    assert cache.calls == cache.errors == 0
    assert not summary["paired_storage"]["ordered_sources_changed"]


@pytest.mark.parametrize("room,discussion", [("other", "d3"), ("project", "d1")])
def test_quality_excludes_other_rooms_and_current_discussion(tmp_path, room, discussion):
    rows, _ = quality(tmp_path, case=_case(query_room=room, query_discussion=discussion))
    assert all(arm["sources"] == [] for arm in rows[0]["arms"].values())


def test_successful_empty_remote_is_measured_not_silently_replaced(tmp_path):
    remote = Remote(bench.VectorModel("a" * 64, 3))
    remote.search_current = lambda *a: ()
    rows, summary = quality(tmp_path, remote=remote)
    assert rows[0]["arms"]["qdrant_hybrid"]["sources"] == []
    assert summary["paired_storage"]["losses"] == [_case()["id"]]
    assert not rows[0]["storage_parity"]["ordered_ids_equal"]


@pytest.mark.parametrize("mode", ["none", "raise"])
def test_swallowed_bm25_fallback_invalidates_measurement(tmp_path, monkeypatch, mode):
    def broken(*a, **k):
        if mode == "raise":
            raise sqlite3.OperationalError("private-provider")
    monkeypatch.setattr(MemoryFTSIndex, "search", broken)
    with pytest.raises(RuntimeError, match="BM25_degraded"):
        quality(tmp_path)


def test_swallowed_scoped_vector_error_invalidates_measurement(tmp_path, monkeypatch):
    original = bench.QdrantMemoryVectorIndex.search_scoped
    calls = [0]
    def fail(self, *args, **kwargs):
        calls[0] += 1
        raise RuntimeError("private-provider")
    monkeypatch.setattr(bench.QdrantMemoryVectorIndex, "search_scoped", fail)
    with pytest.raises(RuntimeError, match="vector_retrieval_degraded"):
        quality(tmp_path)
    assert calls[0] == 1
    monkeypatch.setattr(bench.QdrantMemoryVectorIndex, "search_scoped", original)


def test_swallowed_sqlite_vector_error_invalidates_measurement(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("private-provider")
    monkeypatch.setattr(MemoryVectorIndex, "search", fail)
    with pytest.raises(RuntimeError, match="vector_retrieval_degraded"):
        quality(tmp_path)


def test_cache_miss_never_calls_a_provider(tmp_path):
    cache = embeddings()
    cache.vectors.clear()
    with pytest.raises(RuntimeError, match="cache miss"):
        quality(tmp_path, cache=cache)
    assert cache.calls == 0 and cache.errors == 1


def test_quality_rejects_non_frozen_cache_before_seeding(tmp_path):
    cache = embeddings()
    cache.frozen = False
    with pytest.raises(RuntimeError, match="frozen_cache"):
        quality(tmp_path, cache=cache)
    assert not (tmp_path / "quality.sqlite3").exists()


def test_canonical_mutation_rejected(tmp_path, monkeypatch):
    original = ChatMemoryStore.retrieve
    def change(self, *a, **k):
        result = original(self, *a, **k)
        with self.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_memories SET content=content || ' '")
        return result
    monkeypatch.setattr(ChatMemoryStore, "retrieve", change)
    with pytest.raises(RuntimeError, match="unstable_ordered_sources|quality_canonical_changed"):
        quality(tmp_path)


def test_unstable_ordered_sources_rejected(tmp_path, monkeypatch):
    original = ChatMemoryStore.retrieve
    counter = [0]
    def unstable(self, *a, **k):
        result = original(self, *a, **k)
        counter[0] += 1
        return () if counter[0] == 5 else result
    case = _case()
    case["query"]["content"] = "RabbitMQ"
    monkeypatch.setattr(ChatMemoryStore, "retrieve", unstable)
    with pytest.raises(RuntimeError, match="unstable_ordered_sources"):
        quality(tmp_path, case=case)


@pytest.mark.parametrize("scale", [(1, 1), (10, 10), (10, 35)])
def test_scale_real_counts_no_room_leak_and_clean_audit(tmp_path, scale):
    remote = Remote(bench.VectorModel(bench.SyntheticVectors.model_fingerprint, 1024))
    report = bench.scale_suite(scale, tmp_path / "scale.sqlite3", remote, QdrantEndpoint(), repeats=2, warmups=0)
    assert report["candidates_per_query"] == scale[0] and report["global_points"] == scale[1]
    assert len(remote.points) == scale[1]
    assert report["initial_audit"]["counts"]["current"] == scale[1]
    assert report["initial_audit"]["consistent_at_observation"]
    assert not report["summary"]["paired_storage"]["ordered_sources_changed"]
    assert all(row["storage_parity"]["candidates"] == scale[0] for row in report["rows"])
    assert all(row["arms"]["qdrant_hybrid"]["sources"] == row["expected_sources"] for row in report["rows"])


@pytest.mark.parametrize("change", ["score", "rank", "missing"])
def test_storage_parity_reports_float_or_ranking_drift_not_hides_it(tmp_path, change):
    remote = Remote(bench.VectorModel("a" * 64, 3))
    original = remote.search_current
    def changed(request, mutations):
        hits = original(request, mutations)
        if change == "missing":
            return ()
        if change == "rank":
            return tuple(reversed(hits))
        return tuple(replace(h, similarity=h.similarity-.001) for h in hits)
    remote.search_current = changed
    rows, _ = quality(tmp_path, remote=remote)
    parity = rows[0]["storage_parity"]
    if change == "missing":
        assert parity["sqlite_only_ids"] and not parity["ordered_ids_equal"]
    elif change == "score":
        assert parity["max_score_delta"] == pytest.approx(.001)


@pytest.mark.parametrize("repeats,warmups,scales", [(0, 0, ((10, 10),)), (True, 0, ((10, 10),)),
    (101, 0, ((10, 10),)), (1, -1, ((10, 10),)), (1, 11, ((10, 10),)), (1, 0, ()),
    (1, 0, ((201, 1000),)), (1, 0, ((10, 9),)), (1, 0, ((10, 2001),)),
    (1, 0, ((True, 10),)), (1, 0, ((10, 10), (10, 10))), (1, 0, [(10, 10)])])
def test_invalid_bounds_before_private_or_docker_access(tmp_path, repeats, warmups, scales):
    with pytest.raises(ValueError):
        bench.run_suite(cache=tmp_path / "absent.json", repeats=repeats, warmups=warmups, scales=scales)


def test_default_plan_does_not_open_settings_cache_or_docker(monkeypatch, capsys):
    monkeypatch.setattr(bench, "run_suite", lambda **k: pytest.fail("plan ran experiment"))
    bench.main([])
    assert "No settings/cloud" in capsys.readouterr().out


def test_live_requires_cache_not_implicit_paid_collection():
    with pytest.raises(SystemExit) as exc:
        bench.main(["--live"])
    assert exc.value.code == 2


def test_incomplete_run_has_nonzero_cli_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "run_suite", lambda **k: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as exc:
        bench.main(["--live", "--cache", str(tmp_path / "cache.json")])
    assert exc.value.code == 1


def test_paired_summary_records_wins_losses_and_order_separately():
    a = {"tp": 1, "fp": 0, "sources": ["m1"]}
    b = {"tp": 1, "fp": 0, "sources": ["m1", "m2"]}
    row = {"case_id": "order-only", "arms": {"sqlite_hybrid": a, "qdrant_hybrid": b},
           "storage_parity": {"ordered_ids_equal": False, "max_score_delta": .001}}
    report = bench.paired_summary([row])
    assert report["wins"] == report["losses"] == []
    assert report["ordered_sources_changed"] == report["full_vector_order_changed"] == ["order-only"]


@pytest.mark.parametrize("failure", ["create", "start", "initialize", "quality", "scale", "cleanup", None])
def test_run_checkpoint_cleanup_and_no_partial_quality_claim(tmp_path, monkeypatch, failure):
    calls = []
    def trigger(stage):
        calls.append(stage)
        if failure == stage:
            raise RuntimeError("private-provider-response-and-key")
    class Control:
        name, image_id, url = "synthetic-owned", "sha256:synthetic", "http://127.0.0.1:19001"
        def __init__(self, *args):
            pass
        def create(self):
            trigger("create")
        def start(self):
            trigger("start")
        def cleanup(self):
            trigger("cleanup")
            return True
    class Backend:
        def __init__(self, *args):
            pass
        def initialize(self):
            trigger("initialize")
    def quality_stub(*a, **k):
        trigger("quality")
        return [], {}
    def scale_stub(*a, **k):
        trigger("scale")
        return {"rows": [], "summary": {}}
    cache_path = tmp_path / "cache.json"
    # Test plumbing only; real CLI loader verifies checksum/provenance separately.
    cache_path.write_text("synthetic")
    monkeypatch.setattr(bench, "OwnedDocker", Control)
    monkeypatch.setattr(bench, "QdrantVersionedVectorBackend", Backend)
    monkeypatch.setattr(bench, "validate_unseen", lambda: [_case()])
    monkeypatch.setattr(bench, "verify_frozen", lambda *a: None)
    monkeypatch.setattr(bench.SharedEmbeddings, "load", lambda *a: (embeddings(), {"model": "synthetic", "revision": "test"}))
    monkeypatch.setattr(bench, "quality_suite", quality_stub)
    monkeypatch.setattr(bench, "scale_suite", scale_stub)
    output, manifest = bench.run_suite(cache=cache_path, scales=((1, 1),), output_root=tmp_path)
    assert calls[-1] == "cleanup"
    assert manifest["complete"] is (failure is None)
    assert manifest["cleanup_complete"] is (failure != "cleanup")
    assert manifest["embedding_calls"] == manifest["agent_calls"] == 0
    assert not manifest["default_changed"]
    saved = (output / "manifest.json").read_text()
    assert "private-provider" not in saved
    assert json.loads(saved)["complete"] == manifest["complete"]
    if failure in ("create", "start", "initialize", "quality", "scale"):
        assert not (output / "summary.json").exists()


def test_invalid_cache_never_starts_docker(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text('{"sha256":"invalid"}')
    with pytest.raises(ValueError, match="provenance or checksum"):
        bench.run_suite(cache=path, scales=((1, 1),), output_root=tmp_path)


def test_review_required_before_cache_and_container(tmp_path, monkeypatch):
    lock = tmp_path / "lock.json"
    lock.write_text('{}')
    monkeypatch.setattr(bench, "LOCK_PATH", lock)
    with pytest.raises(ValueError, match="review"):
        bench.run_suite(cache=tmp_path / "absent.json", scales=((1, 1),), output_root=tmp_path)


def test_equal_score_but_full_order_changed_is_reported(tmp_path):
    cache = embeddings()
    cache.vectors[cache.key("审计日志决定按天归档。")] = (1., 0., 0.)
    remote = Remote(bench.VectorModel("a" * 64, 3))
    original = remote.search_current
    remote.search_current = lambda *a: tuple(reversed(original(*a)))
    rows, summary = quality(tmp_path, cache=cache, remote=remote)
    assert not rows[0]["storage_parity"]["ordered_ids_equal"]
    assert summary["paired_storage"]["full_vector_order_changed"] == [_case()["id"]]


def test_aggregate_mrr_and_empty_gold_are_not_counted_as_full_accuracy():
    arm = {"sources": ["noise", "m1"], "tp": 1, "fp": 1, "fn": 0,
           "false_positive_on_empty_gold": False, "samples_ms": [1.]}
    empty = {**arm, "sources": [], "tp": 0, "fp": 0}
    rows = [{"expected_sources": ["m1"], "eligible_candidates": 2, "arms": dict.fromkeys(bench.ARMS, arm)},
            {"expected_sources": [], "eligible_candidates": 0, "arms": dict.fromkeys(bench.ARMS, empty)}]
    report = bench.summarize(rows)["arms"]["qdrant_hybrid"]
    assert report["recall_at_3"] == 1 and report["precision_at_3"] == .5
    assert report["mrr_at_3"] == .5
    assert report["latency_by_candidate_presence"]["nonempty"]["count"] == 1
