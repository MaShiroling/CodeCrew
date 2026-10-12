"""Experiment plumbing only: fake vectors never establish retrieval quality."""

import copy
import json
import sqlite3

import pytest

from app.chat.fts_memory import MemoryFTSIndex
from app.chat.memory import ChatMemoryStore
from app.chat.vector_memory import MemoryVectorIndex
from evals import run_memory_fts_ab as ab
from tests.test_memory_retrieval_ab import FakeEmbedder, _case


def shared():
    inner = FakeEmbedder({
        "后台任务队列决定用 RabbitMQ。": (1.0, 0.0),
        "审计日志决定按天归档。": (0.0, 1.0),
        "异步作业要接哪种消息中间件？": (1.0, 0.0),
    })
    inner.model_fingerprint = "a" * 64
    return ab.SharedEmbeddings(inner)


def test_four_arms_share_embeddings_and_warm_reads_are_offline():
    embeddings = shared()
    row = ab.compare_case(_case(), repeats=3, warmups=2, embeddings=embeddings)
    assert embeddings.calls == embeddings.texts == 3
    assert embeddings.errors == 0
    assert row["same_canonical_snapshot"]
    assert row["eligible_candidates"] == 2
    assert tuple(row["arms"]) == ab.ALL_ARMS
    for arm in ab.ALL_ARMS:
        assert len(row["arms"][arm]["samples_ms"]) == 3
        assert min(row["arms"][arm]["samples_ms"]) >= 0
    assert row["arms"]["legacy_hybrid"]["sources"] == ["m1"]
    assert row["arms"]["bm25_hybrid"]["sources"] == ["m1"]


@pytest.mark.parametrize("room,discussion", [("other", "d3"), ("project", "d1")])
def test_hybrid_preserves_scope(room, discussion):
    row = ab.compare_case(_case(query_room=room, query_discussion=discussion),
                          repeats=1, warmups=0, embeddings=shared())
    for arm in ab.ALL_ARMS:
        assert row["arms"][arm]["sources"] == []


@pytest.mark.parametrize("mode", ["none", "sqlite", "limit"])
def test_bm25_fallback_invalidates_experiment(monkeypatch, mode):
    def fail(*args, **kwargs):
        if mode == "sqlite":
            raise sqlite3.OperationalError("synthetic failure")
        if mode == "limit":
            from app.chat.fts_text import FTSTextLimitError
            raise FTSTextLimitError("synthetic limit")
    monkeypatch.setattr(MemoryFTSIndex, "search", fail)
    with pytest.raises(RuntimeError, match="BM25 degraded"):
        ab.compare_case(_case(), repeats=1, warmups=0)


def test_valid_empty_bm25_is_not_an_experiment_error(monkeypatch):
    monkeypatch.setattr(MemoryFTSIndex, "search", lambda *args, **kwargs: ())
    row = ab.compare_case(_case(), repeats=1, warmups=0)
    assert row["arms"]["bm25"]["sources"] == []


def test_swallowed_vector_error_invalidates_experiment(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic vector error")
    monkeypatch.setattr(MemoryVectorIndex, "search", fail)
    with pytest.raises(RuntimeError, match="vector retrieval degraded"):
        ab.compare_case(_case(), repeats=1, warmups=0, embeddings=shared())


def test_swallowed_indexing_error_invalidates_experiment():
    embeddings = shared()
    embeddings.inner.vectors.clear()
    with pytest.raises(RuntimeError, match="embedding collection degraded"):
        ab.compare_case(_case(), repeats=1, warmups=0, embeddings=embeddings)


def test_canonical_mutation_is_rejected(monkeypatch):
    original = ChatMemoryStore.retrieve
    def mutate(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with self.database.transaction() as connection:
            connection.execute("UPDATE standalone_chat_memories SET content=content || ' '")
        return result
    monkeypatch.setattr(ChatMemoryStore, "retrieve", mutate)
    with pytest.raises(RuntimeError, match="canonical records"):
        ab.compare_case(_case(), repeats=1, warmups=0)


def test_unstable_retrieval_is_rejected(monkeypatch):
    original = ChatMemoryStore.retrieve
    calls = 0
    def unstable(self, *args, **kwargs):
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        return () if calls == 4 else result
    # A query with lexical hits makes the second-round truncation observable.
    case = _case()
    case["query"]["content"] = "RabbitMQ"
    monkeypatch.setattr(ChatMemoryStore, "retrieve", unstable)
    with pytest.raises(RuntimeError, match="ordered sources"):
        ab.compare_case(case, repeats=2, warmups=0)


@pytest.mark.parametrize("repeats,warmups", [(0, 0), (101, 0), (True, 0), (1, -1), (1, 11)])
def test_bounds_rejected(repeats, warmups):
    with pytest.raises(ValueError):
        ab.compare_case(_case(), repeats=repeats, warmups=warmups)


def test_cache_miss_and_call_cap_do_not_make_hidden_requests():
    embeddings = shared()
    embeddings.max_calls = 1
    embeddings.embed(["后台任务队列决定用 RabbitMQ。"])
    embeddings.embed(["后台任务队列决定用 RabbitMQ。"])
    with pytest.raises(RuntimeError, match="cap"):
        embeddings.embed(["审计日志决定按天归档。"])
    assert embeddings.calls == 1
    embeddings.frozen = True
    with pytest.raises(RuntimeError, match="cache miss"):
        embeddings.embed(["异步作业要接哪种消息中间件？"])
    assert embeddings.calls == 1


@pytest.mark.parametrize("vectors", [((1.0,),), ((1.0, float("nan")),),
                                     ((True, 1.0),), (), ((1.0, 0.0), (1.0, 0.0))])
def test_malformed_provider_vectors_rejected(vectors):
    class Broken:
        model_fingerprint = "a" * 64
        def embed(self, texts):
            return vectors
    embeddings = ab.SharedEmbeddings(Broken())
    with pytest.raises(ValueError):
        embeddings.embed(["test"])
    assert embeddings.errors == 1


def test_dimension_change_rejected():
    embeddings = shared()
    embeddings.embed(["后台任务队列决定用 RabbitMQ。"])
    embeddings.inner.vectors["审计日志决定按天归档。"] = (1.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="dimensions"):
        embeddings.embed(["审计日志决定按天归档。"])


def test_cache_roundtrip_never_constructs_settings(tmp_path, monkeypatch):
    embeddings = shared()
    embeddings.embed(["后台任务队列决定用 RabbitMQ。"])
    artifact = embeddings.artifact(identity={"model": "synthetic", "revision": "unit-test"}, complete=True)
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(ab, "Settings", lambda: pytest.fail("private config opened"))
    replay, identity = ab.SharedEmbeddings.load(path)
    assert identity["model"] == "synthetic"
    assert replay.embed(["后台任务队列决定用 RabbitMQ。"])[0] == (1.0, 0.0)
    assert replay.calls == 0
    with pytest.raises(RuntimeError):
        replay.embed(["not cached"])


@pytest.mark.parametrize("change", ["checksum", "dataset", "incomplete", "dimension", "fingerprint"])
def test_bad_cache_rejected(tmp_path, change):
    embeddings = shared()
    embeddings.embed(["后台任务队列决定用 RabbitMQ。"])
    body = embeddings.artifact(identity={"model": "synthetic", "revision": "test"}, complete=True)
    if change == "checksum":
        body["sha256"] = "x"
    else:
        body.pop("sha256")
        if change == "dataset":
            body["dataset_sha256"] = "x"
        elif change == "incomplete":
            body["complete"] = False
        elif change == "dimension":
            body["vectors"]["b" * 64] = [1, 0, 0]
        else:
            body["model_fingerprint"] = "z" * 64
        body["sha256"] = ab._digest(body)
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        ab.SharedEmbeddings.load(path)


def test_summary_quality_and_pairing():
    row = ab.compare_case(_case(), repeats=1, warmups=0)
    other = copy.deepcopy(row)
    other["case_id"] = "gain"
    other["arms"]["bm25"].update(sources=["m1"], tp=1, fp=0, fn=0)
    summary = ab.summarize([row, other])
    assert summary["arms"]["bm25"]["recall_at_3"] == 0.5
    assert summary["arms"]["bm25"]["mrr_at_3"] == 0.5
    assert summary["pairs"]["legacy_vs_bm25"]["wins"] == ["gain"]
    assert summary["pairs"]["legacy_vs_bm25"]["ties"] == 1


def test_full_default_suite_offline_and_freeze_unchanged(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(ab, "Settings", lambda: pytest.fail("private config opened"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network opened"))
    original_seed = ab._seed_case
    paths = []
    def seeded(case, path):
        paths.append(path)
        return original_seed(case, path)
    monkeypatch.setattr(ab, "_seed_case", seeded)
    frozen_hash = ab._sha256(ab.FROZEN_PATH)
    path = ab.run_suite(output_root=tmp_path, repeats=1, warmups=0)
    manifest = json.loads((path / "manifest.json").read_text())
    summary = json.loads((path / "summary.json").read_text())
    assert manifest["complete"]
    assert manifest["embedding_calls"] == manifest["agent_calls"] == 0
    assert manifest["mode"] == "offline_lexical"
    assert not (path / "embedding-cache.json").exists()
    assert ab._sha256(ab.FROZEN_PATH) == frozen_hash
    assert all(not seed.exists() for seed in paths)
    assert summary["arms"]["legacy"]["tp"] == 10
    assert summary["arms"]["bm25"]["tp"] == 10
    assert summary["pairs"]["legacy_vs_bm25"]["ordered_sources_changed"] == []


def test_partial_experiment_has_incomplete_manifest_no_summary(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure with secret-like text")
    monkeypatch.setattr(ab, "compare_case", fail)
    with pytest.raises(RuntimeError):
        ab.run_suite(output_root=tmp_path, repeats=1, warmups=0)
    path = next(tmp_path.iterdir())
    manifest = json.loads((path / "manifest.json").read_text())
    assert not manifest["complete"]
    assert manifest["error_type"] == "RuntimeError"
    assert "secret-like" not in (path / "manifest.json").read_text()
    assert not (path / "summary.json").exists()


def test_collect_then_replay_cli_pipeline_without_live_api(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    settings = SimpleNamespace(
        memory_embedding_mode="on", memory_embedding_base_url="https://example.test/v1",
        memory_embedding_model="synthetic-test", memory_embedding_revision="test",
        memory_embedding_api_key=SecretStr("never-write-this-key"),
        memory_embedding_timeout_seconds=5,
    )
    class SyntheticHTTP:
        # All-constant vectors are deliberately unsuitable for quality evaluation.
        def __init__(self, endpoint, **kwargs):
            self.model_fingerprint = endpoint.model_fingerprint
        def embed(self, texts):
            return tuple((1.0, 0.0) for _ in texts)
    monkeypatch.setattr(ab, "Settings", lambda: settings)
    monkeypatch.setattr(ab, "OpenAICompatibleEmbedder", SyntheticHTTP)
    collected = ab.run_suite(output_root=tmp_path, embed=True, repeats=1, warmups=0)
    manifest = json.loads((collected / "manifest.json").read_text())
    assert manifest["complete"] and 0 < manifest["embedding_calls"] <= 50
    assert "never-write-this-key" not in (collected / "manifest.json").read_text()
    assert "never-write-this-key" not in (collected / "embedding-cache.json").read_text()
    monkeypatch.setattr(ab, "Settings", lambda: pytest.fail("settings read on replay"))
    replayed = ab.run_suite(output_root=tmp_path, cache=collected / "embedding-cache.json",
                           repeats=1, warmups=0)
    replay_manifest = json.loads((replayed / "manifest.json").read_text())
    assert replay_manifest["complete"] and replay_manifest["embedding_calls"] == 0
    a = json.loads((collected / "summary.json").read_text())
    b = json.loads((replayed / "summary.json").read_text())
    for arm in ab.ALL_ARMS:
        assert a["arms"][arm]["tp"] == b["arms"][arm]["tp"]
        assert a["arms"][arm]["fp"] == b["arms"][arm]["fp"]
    assert a["pairs"] == b["pairs"]
