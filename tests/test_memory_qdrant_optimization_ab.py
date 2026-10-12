"""A/B validity with real orchestration + fake HTTP; no performance claims."""

import json
from collections import Counter

import pytest

from evals import benchmark_memory_qdrant as bench
from evals import benchmark_memory_qdrant_optimization as ab
from tests.test_memory_qdrant_benchmark import embeddings
from tests.test_memory_retrieval_ab import _case
from tests.test_qdrant_vector_sync import ConditionalREST


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(bench, "OwnedDocker", lambda *a, **k: pytest.fail("unexpected Docker"))


def quality(tmp_path, *, repeats=2, warmups=1, case=None):
    cache = embeddings()
    endpoint = bench.QdrantEndpoint()
    backend = bench.QdrantVersionedVectorBackend(endpoint, bench.VectorModel(cache.model_fingerprint, 3))
    backend._opener = ConditionalREST(backend.model)
    backend.initialize()
    return bench.quality_suite([case or _case()], tmp_path / "fixture.sqlite3", cache, backend, endpoint,
        repeats=repeats, warmups=warmups, read_runner=ab.factorial_reads, progress=lambda *a, **k: None)


def test_all_factorial_arms_same_sources_full_rank_and_matched_samples(tmp_path):
    rows, _ = quality(tmp_path)
    row = rows[0]
    assert tuple(row["arms"]) == ab.ARMS
    for arm in ab.REMOTE_ARMS:
        assert row["arms"][arm]["full_rank_equal"]
        assert row["arms"][arm]["sources"] == row["arms"]["qdrant_hybrid"]["sources"]
    assert all(len(row["arms"][arm]["samples_ms"]) == 2 for arm in ab.ARMS)
    assert row["arms"]["qdrant_hybrid"]["measured_round_orders"] == [ab.arm_order(1), ab.arm_order(2)]


def test_diagnostic_sql_http_shapes_excluded_from_official_samples(tmp_path):
    rows, _ = quality(tmp_path)
    ab.check_scale_counts(rows, 2)
    for arm in ab.REMOTE_ARMS:
        assert row_sample_count(rows, arm) == 2
        assert rows[0]["arms"][arm]["diagnostic"]["calls"]["retrieve_other"] == 1


def row_sample_count(rows, arm):
    return sum(len(row["arms"][arm]["samples_ms"]) for row in rows)


def test_no_candidates_separately_reported_no_metadata_or_query(tmp_path):
    rows, _ = quality(tmp_path, case=_case(query_room="other", query_discussion="d3"))
    summary = ab.summarize(rows)
    assert summary["nonempty_remote_cases"] == 0 and summary["short_circuit_cases"] == 1
    assert summary["paired_nonempty"] == []
    assert summary["latencies_nonempty"]["qdrant_hybrid"]["count"] == 0
    for a in ab.REMOTE_ARMS:
        assert not rows[0]["arms"][a]["diagnostic"]["calls"].get("http_query")


def test_every_arm_has_balanced_position_over_twelve_rounds():
    for offset in (0, 1, 13):
        counts = {a: Counter() for a in ab.ARMS}
        for i in range(24):
            order = ab.arm_order(i, offset)
            assert set(order) == set(ab.ARMS)
            for pos, arm in enumerate(order):
                counts[arm][pos] += 1
        assert all(set(c.values()) == {4} and len(c) == 6 for c in counts.values())


def effect_rows(a, b):
    return [{"arms": {"a": {"samples_ms": a}, "b": {"samples_ms": b}}}]


def test_paired_statistics_sign_percent_pairing_and_deterministic_cluster_ci():
    rows = effect_rows([10., 20.], [6., 12.])
    result = ab.paired_effect(rows, "a", "b")
    assert result["pairs"] == 2 and result["query_clusters"] == 1
    assert result["mean_saved_ms"] == result["median_paired_saved_ms"] == 6
    assert result["mean_reduction_pct"] == 40
    assert result["query_cluster_mean_saved_ci95_ms"] == [6, 6]
    assert result == ab.paired_effect(rows, "a", "b")
    reverse = ab.paired_effect(rows, "b", "a")
    assert reverse["mean_saved_ms"] == -6 and reverse["candidate_faster_fraction"] == 0


@pytest.mark.parametrize("a,b", [([], []), ([1], [1, 2]), ([0], [1]), ([float('nan')], [1]),
    ([float('inf')], [1]), ([True], [1]), ([1], [-1])])
def test_invalid_paired_statistics_rejected(a, b):
    with pytest.raises(ValueError):
        ab.paired_effect(effect_rows(a, b), "a", "b")


def test_cluster_ci_does_not_treat_rounds_as_independent_queries():
    rows = effect_rows([10.] * 24, [9.] * 24) + effect_rows([10.] * 24, [13.] * 24)
    result = ab.paired_effect(rows, "a", "b")
    assert result["query_clusters"] == 2 and result["pairs"] == 48
    assert result["mean_saved_ms"] == -1
    assert result["query_cluster_mean_saved_ci95_ms"] == [-3, 1]


def test_full_rank_drift_is_not_hidden_by_final_top_three(tmp_path, monkeypatch):
    original = ab.RowwiseIndex.search_scoped
    def wrong(self, *args, **kwargs):
        return tuple(reversed(original(self, *args, **kwargs)))
    monkeypatch.setattr(ab.RowwiseIndex, "search_scoped", wrong)
    with pytest.raises(RuntimeError, match="control_full_rank_changed"):
        quality(tmp_path)


def test_swallowed_vector_error_invalidates_before_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(ab.RowwiseIndex, "search_scoped", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("safe failure")))
    with pytest.raises(RuntimeError):
        quality(tmp_path)


def test_default_only_plan_no_cache_or_docker(monkeypatch, capsys):
    monkeypatch.setattr(ab, "run_suite", lambda **k: pytest.fail("plan executed"))
    ab.main([])
    assert "Plan only" in capsys.readouterr().out


def test_live_requires_explicit_cache():
    with pytest.raises(SystemExit) as caught:
        ab.main(["--live"])
    assert caught.value.code == 2


def test_incomplete_cli_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "run_suite", lambda **k: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as caught:
        ab.main(["--live", "--cache", str(tmp_path / "cache.json")])
    assert caught.value.code == 1


@pytest.mark.parametrize("repeats,warmups", [(0, 1), (True, 1), (101, 0), (1, -1), (1, 11)])
def test_bounds_reject_before_cache_and_docker(tmp_path, repeats, warmups):
    with pytest.raises(ValueError):
        ab.run_suite(cache=tmp_path / "absent", repeats=repeats, warmups=warmups)


def test_failed_measurement_owned_cleanup_and_manifest(tmp_path, monkeypatch):
    # No real Docker; failure inside the owned lifecycle must still attempt cleanup.
    class Control:
        image_id, name, url = "offline", "offline", "http://127.0.0.1:1"
        def __init__(self, path):
            self.cleaned = False
        def create(self):
            pass
        def start(self):
            raise RuntimeError("private-provider-body")
        def cleanup(self):
            return True
    monkeypatch.setattr(bench, "OwnedDocker", Control)
    monkeypatch.setattr(bench.SharedEmbeddings, "load", lambda p: (embeddings(), {}))
    monkeypatch.setattr(bench, "verify_frozen", lambda *a: None)
    output, manifest = ab.run_suite(cache=bench.CASES_PATH, repeats=1, warmups=0, output_root=tmp_path)
    assert not manifest["complete"] and manifest["cleanup_complete"]
    saved = json.loads((output / "manifest.json").read_text())
    assert saved["error_type"] == "RuntimeError"
    assert "private-provider" not in json.dumps(saved)
