"""Offline threshold sweeps reuse frozen text, vectors, and production fusion."""

import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

import pytest

from app.chat.memory import ChatMemory
from app.chat.vector_memory import EmbeddingEndpoint
from evals import sweep_memory_thresholds as threshold_sweep
from evals.sweep_memory_thresholds import (
    EmbeddingCache,
    Scenario,
    _hash,
    _parse_thresholds,
    _semantic_texts,
    load_candidate,
    sweep,
)


class _FakeEmbedder:
    model_fingerprint = "fake-model-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return tuple((1.0, float(index + 1)) for index, _text in enumerate(texts))


def _memory(content: str) -> ChatMemory:
    return ChatMemory(
        room_id=uuid4(), source_message_id=uuid4(), content=content,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_threshold_sweep_exposes_recall_vs_irrelevant_false_positive(tmp_path: Path) -> None:
    memory = _memory("默认中文作答")
    positive = Scenario(
        scenario_id="positive", kind="frozen_case", query="交流语言是什么",
        memories=(memory,), source_by_id={memory.memory_id: "m1"},
        expected=frozenset({"m1"}), observed_lexical=(),
    )
    negative = Scenario(
        scenario_id="negative", kind="counterfactual_irrelevant",
        query="奶茶甜度多少", memories=(memory,),
        source_by_id={memory.memory_id: "m1"}, expected=frozenset(),
        reason="Language preference does not answer tea sweetness.",
    )
    cache = EmbeddingCache(tmp_path / "vectors.json", "fake-model-v1")
    cache.vectors = {
        _hash(memory.content): (1.0, 0.0),
        _hash("交流语言是什么"): (0.55, math.sqrt(1 - 0.55**2)),
        _hash("奶茶甜度多少"): (0.52, math.sqrt(1 - 0.52**2)),
    }
    report = sweep((positive, negative), cache, (0.50, 0.54, 0.60))
    lexical, permissive, balanced, strict = report["thresholds"]
    assert lexical["frozen_cases"]["fn"] == 1
    assert permissive["frozen_cases"]["tp"] == 1
    assert permissive["counterfactual_irrelevant"]["fp"] == 1
    assert balanced["frozen_cases"]["tp"] == 1
    assert balanced["counterfactual_irrelevant"]["fp"] == 0
    assert strict["frozen_cases"]["fn"] == 1
    assert balanced["scenarios"][0]["recalled_sources"] == ["m1"]


def test_embedding_cache_batches_at_ten_and_reuses_existing_vectors(tmp_path: Path) -> None:
    cache_file = tmp_path / "vectors.json"
    cache = EmbeddingCache(cache_file, "fake-model-v1")
    embedder = _FakeEmbedder()
    texts = tuple(f"test text {index}" for index in range(12))
    assert cache.ensure(texts, embedder) == 12
    assert [len(call) for call in embedder.calls] == [10, 2]
    restarted = EmbeddingCache(cache_file, "fake-model-v1")
    assert restarted.ensure(texts, embedder) == 0
    assert [len(call) for call in embedder.calls] == [10, 2]
    with pytest.raises(ValueError, match="model/version"):
        EmbeddingCache(cache_file, "different-model")
    assert "test text" not in cache_file.read_text(encoding="utf-8")


def test_sensitive_scenarios_never_request_cloud_embeddings(tmp_path: Path) -> None:
    memory = _memory("测试 api_key 为占位符")
    scenario = Scenario(
        scenario_id="secret", kind="frozen_case", query="我的 api_key 是什么",
        memories=(memory,), source_by_id={memory.memory_id: "m1"},
        expected=frozenset(),
    )
    assert _semantic_texts((scenario,)) == ()
    cache = EmbeddingCache(tmp_path / "vectors.json", "fake-model-v1")
    assert sweep((scenario,), cache, (0.5,))["thresholds"][1]["mode"] == "hybrid"


def test_holdout_candidate_is_locked_to_tuning_evidence(tmp_path: Path) -> None:
    endpoint = EmbeddingEndpoint(
        base_url="https://example.test/v1", model="fake-embed", revision="v1",
        api_key="test-only",
    )
    report = tmp_path / "frozen-tuning.json"
    report.write_text(json.dumps({
        "split": "tuning", "source_run": "/tmp/frozen-tuning",
        "thresholds": [{"threshold": 0.5}],
    }), encoding="utf-8")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "version": 1, "threshold": 0.5,
        "status": "provisional_holdout_candidate",
        "model_fingerprint": endpoint.model_fingerprint,
        "model": endpoint.model, "revision": endpoint.revision,
        "dataset_sha256": hashlib.sha256(threshold_sweep.CASES_PATH.read_bytes()).hexdigest(),
        "controls_sha256": hashlib.sha256(threshold_sweep.CONTROLS_PATH.read_bytes()).hexdigest(),
        "tuning_run": "frozen-tuning", "tuning_report": report.name,
        "tuning_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    assert load_candidate(candidate, endpoint, report_root=tmp_path) == 0.5
    report.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen evidence"):
        load_candidate(candidate, endpoint, report_root=tmp_path)


@pytest.mark.parametrize("raw", ["", "0.5,0.5", "nan", "1.1", "oops"])
def test_invalid_threshold_lists_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="thresholds"):
        _parse_thresholds(raw)
