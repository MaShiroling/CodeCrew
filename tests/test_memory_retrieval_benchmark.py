"""Latency and token accounting stay distinct from actual cloud billing."""

from decimal import Decimal

import pytest

from evals.benchmark_memory_retrieval import (
    MeteredEmbedder,
    UsageRecorder,
    benchmark_case,
    estimate_original_price,
    latency_summary,
    summarize,
)


class FakeEmbedder:
    model_fingerprint = "offline-benchmark-v1"

    def __init__(self, usage: UsageRecorder) -> None:
        self.usage = usage

    def embed(self, texts):
        self.usage.record(7)
        return tuple((1.0, 0.0) for _ in texts)


def _case() -> dict:
    return {
        "id": "dev_offline_benchmark", "domain": "development",
        "messages": [{"id": "m1", "room": "project", "discussion": "d1",
                      "role": "human", "content": "@白金 内部协议决定用 gRPC。"}],
        "query": {"room": "project", "discussion": "d2",
                  "content": "@月见 服务通信选什么协议？"},
        "gold": {"writes": [{"source": "m1", "kind": "decision",
                             "fact": "内部协议决定用 gRPC。"}],
                 "active_sources": ["m1"], "recall_sources": ["m1"]},
    }


def test_benchmark_separates_index_cold_and_warm_cloud_calls() -> None:
    usage = UsageRecorder()
    embedder = MeteredEmbedder(FakeEmbedder(usage), max_calls=10)
    result = benchmark_case(_case(), embedder, min_similarity=0.65, repeats=2)
    assert result["lexical_sources"] == ["m1"]
    assert result["hybrid_sources"] == ["m1"]
    assert len(result["samples_ms"]["lexical"]) == 2
    assert len(result["samples_ms"]["write_index"]) == 1
    assert len(result["samples_ms"]["warm_cloud"]) == 2
    assert len(result["samples_ms"]["cold_cloud"]) == 1
    assert embedder.calls == 5  # index + 2 warm queries + cold backfill + cold query
    summary = summarize([result], embedder, usage,
                        price_per_million_cny=Decimal("0.5"))
    assert summary["api_usage_total_tokens"] == 35
    assert summary["api_usage_complete"] is True
    assert summary["original_price_estimate_cny"] == "0.00001750"
    assert summary["actual_billed_cny"] is None


def test_estimate_requires_complete_provider_usage() -> None:
    assert estimate_original_price(
        100, price_per_million_cny=Decimal("0.5"), usage_complete=False,
    ) is None
    assert latency_summary([1.0, 2.0, 3.0, 4.0, 5.0])["p95_ms"] == 5.0


def test_benchmark_caps_cloud_requests() -> None:
    usage = UsageRecorder()
    embedder = MeteredEmbedder(FakeEmbedder(usage), max_calls=1)
    with pytest.raises(RuntimeError, match="embedding failed"):
        benchmark_case(_case(), embedder, min_similarity=0.65, repeats=2)
    assert embedder.calls == 1


def test_indexing_includes_memory_outside_query_room() -> None:
    case = _case()
    case["query"]["room"] = "other"
    case["gold"]["recall_sources"] = []
    usage = UsageRecorder()
    embedder = MeteredEmbedder(FakeEmbedder(usage), max_calls=10)
    result = benchmark_case(case, embedder, min_similarity=0.65, repeats=1)
    assert result["active_memories_in_query_room"] == 0
    assert result["active_memories_total"] == 1
    assert len(result["samples_ms"]["write_index"]) == 1
    assert len(result["samples_ms"]["warm_local"]) == 1
    assert len(result["samples_ms"]["cold_local"]) == 1
