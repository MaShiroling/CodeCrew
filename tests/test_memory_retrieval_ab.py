"""Paired memory retrieval measures ranking, not stochastic Agent output."""

import json

import pytest

from evals.run_memory_retrieval_ab import (
    CountingEmbedder,
    _require_review,
    compare_case,
    summarize,
)
from evals.validate_memory_unseen import LOCK_PATH


class FakeEmbedder:
    model_fingerprint = "offline-test-v1"

    def __init__(self, vectors: dict[str, tuple[float, float]]) -> None:
        self.vectors = vectors

    def embed(self, texts):
        return tuple(self.vectors[text] for text in texts)


def _case(*, query_room: str = "project", query_discussion: str = "d3") -> dict:
    return {
        "id": "dev_semantic_pair", "domain": "development",
        "messages": [
            {"id": "m1", "room": "project", "discussion": "d1", "role": "human",
             "content": "@白金 后台任务队列决定用 RabbitMQ。"},
            {"id": "m2", "room": "project", "discussion": "d2", "role": "human",
             "content": "@白金 审计日志决定按天归档。"},
        ],
        "query": {"room": query_room, "discussion": query_discussion,
                  "content": "@月见 异步作业要接哪种消息中间件？"},
        "gold": {
            "writes": [
                {"source": "m1", "kind": "decision", "fact": "后台任务队列决定用 RabbitMQ。"},
                {"source": "m2", "kind": "decision", "fact": "审计日志决定按天归档。"},
            ],
            "active_sources": ["m1", "m2"],
            "recall_sources": ["m1"] if query_room == "project"
            and query_discussion != "d1" else [],
        },
    }


def _embedder() -> CountingEmbedder:
    return CountingEmbedder(FakeEmbedder({
        "后台任务队列决定用 RabbitMQ。": (1.0, 0.0),
        "审计日志决定按天归档。": (0.0, 1.0),
        "异步作业要接哪种消息中间件？": (1.0, 0.0),
    }))


def test_paired_retrieval_has_identical_seed_and_semantic_gain() -> None:
    embedder = _embedder()
    result = compare_case(_case(), embedder, min_similarity=0.8)
    assert result["same_seeded_memory_snapshot"] is True
    assert result["lexical"]["sources"] == []
    assert result["hybrid"]["sources"] == ["m1"]
    assert summarize([result])["paired_wins"] == ["dev_semantic_pair"]
    assert embedder.calls == 3  # two production-style writes, one query
    assert embedder.texts == 3


def test_paired_retrieval_respects_room_boundary() -> None:
    case = _case(query_room="other")
    result = compare_case(case, _embedder(), min_similarity=0.8)
    assert result["lexical"]["sources"] == []
    assert result["hybrid"]["sources"] == []
    assert result["hybrid"]["false_positive_on_empty_gold"] is False


def test_paired_retrieval_respects_same_discussion_boundary() -> None:
    case = _case(query_discussion="d1")
    result = compare_case(case, _embedder(), min_similarity=0.8)
    assert result["lexical"]["sources"] == []
    assert result["hybrid"]["sources"] == []


def test_unreviewed_labels_cannot_trigger_billable_run() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    _require_review(lock)
    lock["label_provenance"] = "agent_draft_pending_independent_human_review"
    with pytest.raises(ValueError, match="human label review"):
        _require_review(lock)
