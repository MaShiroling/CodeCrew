"""Synthetic adapter/embedding/remote controls; no paid or actual OS evidence."""

import copy
import json
import urllib.request
from dataclasses import replace

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.vector_memory import EmbeddingError
from evals import run_memory_persistent_live as probe
from tests.test_memory_live_worker import target_fixture
from tests.test_memory_qdrant_faults import OfflineStorage


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))


class Embeddings:
    def __init__(self, fingerprint="synthetic"):
        self.model_fingerprint, self.calls = fingerprint, 0
    def embed(self, texts):
        self.calls += 1
        return tuple((1., 0.) for text in texts)


def test_plan_opens_no_target_keys_or_model(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_target", lambda *a, **k: pytest.fail("target loaded"))
    monkeypatch.setattr(probe, "dotenv_values", lambda *a: pytest.fail("keys loaded"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


def test_embedding_budget_caps_attempts_including_failures():
    inner = Embeddings()
    capped = probe.BudgetEmbedder(inner, 2, limit=2)
    capped.embed(["synthetic"])
    capped.embed(["synthetic"])
    with pytest.raises(EmbeddingError, match="budget"):
        capped.embed(["synthetic"])
    assert inner.calls == capped.calls == 2 and capped.errors == 1


def test_dimension_drift_not_silently_accepted():
    capped = probe.BudgetEmbedder(Embeddings(), 1024)
    with pytest.raises(EmbeddingError, match="dimension"):
        capped.embed(["synthetic"])
    assert capped.calls == capped.errors == 1


@pytest.mark.parametrize("limit", [True, 0, 41, -1])
def test_embedding_budget_bounds(limit):
    with pytest.raises(ValueError):
        probe.BudgetEmbedder(Embeddings(), 2, limit=limit)


def test_fixture_scopes_and_twelve_steps():
    assert len(probe.STEPS) == 12
    assert {d for _, d, _, _ in probe.STEPS} == {"dev", "personal"}
    assert [kind for _, _, _, kind in probe.STEPS].count("complete") == 1
    assert [kind for _, _, _, kind in probe.STEPS].count("cancel") == 1


class Adapter(FakeAgentAdapter):
    def __init__(self, *, missing_write=False):
        super().__init__()
        self.starts, self.missing_write = 0, missing_write
    async def start(self, request):
        self.starts += 1
        line = next(line for line in request.prompt.splitlines() if line.startswith("上下文摘录（JSON）："))
        context = json.loads(line.split("：", 1)[1])["room_memories"]
        received = request.prompt.split("收到的消息：", 1)[1].splitlines()[0]
        step = next((s for s in probe.STEPS if s[2] in received), None)
        assert step is not None
        name, _, content, kind = step
        payload = {"content": "收到", "handoff_to": [], "memory_proposal": None, "memory_action": None}
        if kind in {"decision", "correction", "open_loop"} and not self.missing_write:
            payload["memory_proposal"] = {"kind": kind, "content": content}
            if kind == "correction":
                payload["memory_proposal"]["replaces_memory_id"] = next(m["memory_id"] for m in context if m["kind"] == "decision")
        elif kind in {"complete", "cancel"}:
            payload["memory_action"] = {"action": kind, "memory_id": next(m["memory_id"] for m in context if m["kind"] == "open_loop"), "evidence": content}
        elif kind == "query":
            payload["content"] = {"recall_decision": "8 秒", "recall_replacement": "12 秒",
                "other_room_isolation": "本房间未知", "closed_personal_query": "已取消"}.get(name, "还需要安排 " + context[-1]["content"] if context else "没有记录")
        self._scenario = FakeAgentScenario(output={"message": json.dumps(payload, ensure_ascii=False)})
        return await super().start(request)


def journey_fixture(tmp_path, *, missing_write=False):
    target, _, _ = target_fixture(tmp_path)
    remote = OfflineStorage()
    remote.model, remote.sync_target_id = target.backend.model, target.backend.sync_target_id
    remote.endpoint = QdrantEndpoint(
        collection_prefix="codecrew_live_real_" + "a" * 16)
    # Match snapshot target IDs without ever contacting this synthetic endpoint.
    target = replace(target, backend=remote)
    embedder = probe.BudgetEmbedder(Embeddings(remote.model.fingerprint), remote.model.dimensions)
    adapter = Adapter(missing_write=missing_write)
    async def delivered(memory):
        journey.worker.run_once(limit=32)  # Offline driver only, not the live parent.
    journey = probe.Journey(target, embedder, adapter, tmp_path / "execution", delivered)
    def backend(model):
        selected = copy.copy(remote)
        selected.model = model
        return selected
    journey.index._backend = backend
    return journey, remote


@pytest.mark.asyncio
async def test_complete_synthetic_journey_covers_real_dispatch_and_guard_state(tmp_path):
    journey, remote = journey_fixture(tmp_path)
    assert await journey.run()
    assert len(journey.steps) == 12 and all(s["passed"] for s in journey.steps)
    assert len(journey.proposals) == 4 and len(journey.actions) == 2
    assert len(remote.points) == 4
    assert sum(m.record is None for m in remote.points.values()) == 3
    assert journey.adapter.starts <= 12 and journey.embedder.calls <= 40
    assert probe.finish_audit(journey.worker)["consistent_at_observation"]


@pytest.mark.asyncio
async def test_smoke_is_only_two_steps_not_full_lifecycle(tmp_path):
    journey, _ = journey_fixture(tmp_path)
    assert await journey.run(smoke=True)
    assert len(journey.steps) == 2 and journey.adapter.starts == 2


@pytest.mark.asyncio
async def test_failed_write_stops_without_paid_retry_or_manual_seed(tmp_path):
    journey, remote = journey_fixture(tmp_path, missing_write=True)
    with pytest.raises(ValueError, match="expected_auto_write"):
        await journey.run()
    assert len(journey.steps) == 1 and journey.adapter.starts == 1
    assert not remote.points and not journey.store.memory.list_active(journey.rooms["dev"])


def test_live_parent_never_delivers_or_seeds_memory():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(probe.__file__).read_text())
    for node in (n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {"run_live", "_run_locked"}):
        calls = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"run_once", "claim", "create_memory", "capture_agent_proposal", "capture_agent_action", "register_target"}
