"""Offline harness validity; fake remote/delivery is not actual worker evidence."""

import urllib.request

import pytest

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.vector_outbox import MemoryVectorOutbox
from evals import check_memory_vector_worker_journey as probe
from tests.test_memory_qdrant_faults import OfflineStorage


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))


def fixture(tmp_path, lexical="bm25"):
    import copy
    remote = OfflineStorage()
    journey = probe.WriterJourney(tmp_path / "chat.sqlite3", remote, QdrantEndpoint(), remote, lexical_backend=lexical)
    def backend(model):
        selected = copy.copy(remote)
        selected.model = model
        return selected
    journey.index._backend = backend
    # Offline test driver only. Live harness never claims/delivers from parent.
    worker = MemoryVectorOutbox(journey.database, remote)
    def delivered(memory):
        worker.run_once(limit=32)
        with journey.database.connect() as conn:
            status = conn.execute("SELECT status FROM standalone_chat_vector_outbox WHERE target_id=? AND memory_id=? "
                "ORDER BY revision DESC LIMIT 1", (worker.target_id, str(memory.memory_id))).fetchone()[0]
        assert status == "delivered"
    def waiting(memory):
        worker.run_once(limit=32)
        assert worker.status()["waiting"] == 1
    return journey, remote, delivered, waiting


def test_plan_has_no_settings_process_or_docker(monkeypatch, capsys):
    monkeypatch.setattr(probe, "run_suite", lambda **k: pytest.fail("plan launched resources"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.parametrize("lexical", ["legacy", "bm25"])
def test_writer_lifecycle_uses_automatic_cache_and_validates_boundaries(tmp_path, lexical, monkeypatch):
    journey, remote, delivered, waiting = fixture(tmp_path, lexical)
    monkeypatch.setattr(journey, "save", lambda *a, **k: pytest.fail("manual fixture cache seed used"))
    monkeypatch.setattr(journey, "cache", lambda *a, **k: pytest.fail("manual fixture cache used"))
    updated, missing = journey.lifecycle(delivered, waiting)
    assert len(journey.checks) == 6
    assert journey.store.memory.list_active(journey.room) == (missing, updated)
    assert len(remote.points) == 6  # 3 active + replaced/completed/cancelled tombstones.
    before = journey.fingerprint()
    journey.restart_store()
    assert journey.fingerprint() == before
    journey.semantic(journey.room, (updated, missing))
    journey.context(journey.room, (updated, missing))
    journey.closed_after_restart()
    audit = probe.finish_audit(journey.worker)
    assert audit["consistent_at_observation"]


def test_lexical_success_cannot_mask_missing_remote_delivery(tmp_path):
    journey, _, _, _ = fixture(tmp_path)
    memory, _, _ = journey.write("gateway 网关固定 8 秒。")
    assert journey.store.memory.retrieve(journey.room, "gateway") == (memory,)
    with pytest.raises(RuntimeError, match="pure_vector_recall_mismatch"):
        journey.semantic(journey.room, (memory,))


def test_remote_revision_drift_rejected_even_if_lexical_recall_works(tmp_path):
    journey, remote, delivered, _ = fixture(tmp_path)
    memory, _, _ = journey.write("gateway 网关固定 8 秒。")
    delivered(memory)
    remote.points.clear()
    with pytest.raises(RuntimeError, match="remote_revision_or_tombstone_mismatch"):
        journey.remote(memory)


def test_semantic_probe_refuses_accidental_keyword_overlap(tmp_path):
    journey, _, delivered, _ = fixture(tmp_path)
    memory, _, _ = journey.write("semanticprobe 网关固定 8 秒。")
    delivered(memory)
    with pytest.raises(RuntimeError, match="semantic_probe_has_legacy_match"):
        journey.semantic(journey.room, (memory,))


def test_rejected_agent_action_leaves_active_cache_unchanged(tmp_path):
    from uuid import uuid4

    from app.chat.memory import AgentMemoryAction
    journey, _, delivered, _ = fixture(tmp_path)
    memory, _, _ = journey.write("待办：完成 gateway loadtest 压测。", kind="open_loop")
    delivered(memory)
    source = journey.service.post_message(journey.room, content="@白金 gateway loadtest 压测完成了吗？",
                                         idempotency_key=uuid4(), reply_to=None).message
    before = journey.fingerprint()
    result = journey.store.memory.capture_agent_action(journey.room, source.message_id,
        AgentMemoryAction(action="complete", memory_id=memory.memory_id, evidence="gateway loadtest 压测完成了吗？"))
    assert result is None and journey.fingerprint() == before
    journey.semantic(journey.room, (memory,))


def test_live_parent_never_calls_delivery_or_fixture_seed():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(probe.__file__).read_text())
    live = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiveJourney")
    calls = {n.func.attr for n in ast.walk(live) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"run_once", "claim", "cache", "save", "retry_dead", "apply_mutation"}
