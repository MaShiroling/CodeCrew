"""Pinned recovery controls on a temporary target; no models or real remote."""

import hashlib
import json
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.chat.vector_memory import OpenAICompatibleEmbedder
from evals.memory_persistent_resume import database_snapshot
from scripts import recover_memory_live_miswrite as recovery
from tests.test_memory_persistent_live import journey_fixture


@pytest.fixture(autouse=True)
def no_network_or_models(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("paid embedding"))


def fixture(tmp_path, monkeypatch):
    journey, remote = journey_fixture(tmp_path)
    room = journey.service.create_room(title="recovery", idempotency_key=uuid4())
    source = journey.service.post_message(room.room_id, content="@月见 本房间是否已确定 API 网关读超时？没有记录就说明未知。",
        idempotency_key=uuid4(), reply_to=None)
    # Test-owned manual seed of the already reviewed bad state; future auto
    # proposal guards must remain strict. Recovery itself never creates facts.
    memory = journey.store.memory.create(room.room_id, source.message.message_id, "没有记录就说明未知")
    with journey.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET kind='preference',origin='agent_auto' WHERE memory_id=?",
                     (str(memory.memory_id),))
    monkeypatch.setattr(recovery, "BAD_ID", memory.memory_id)
    report = {"manifest_sha256": journey.target.manifest_sha256, "database": str(journey.target.database),
        "worker_cleanup_complete": True, "worker_exit": 0, "rooms": {"personal": str(room.room_id)},
        "steps": [{} for _ in range(11)] + [{"source": str(source.message.message_id)}],
        "database_snapshot": database_snapshot(journey.target.database)}
    path = journey.target.directory / "live-runs" / recovery.REVIEWED_RUN / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))
    monkeypatch.setattr(recovery, "REVIEWED_SHA", hashlib.sha256(path.read_bytes()).hexdigest())
    return journey, remote, path, report


def test_plan_opens_nothing(monkeypatch, capsys):
    monkeypatch.setattr(recovery, "load_target", lambda *a: pytest.fail("target opened"))
    recovery.main([])
    assert "Plan only" in capsys.readouterr().out


def test_only_reviewed_single_record_retired_with_backup_and_audit(tmp_path, monkeypatch):
    journey, remote, path, original = fixture(tmp_path, monkeypatch)
    raw = path.read_bytes()
    directory, report = recovery.repair(journey.target)
    assert report["complete"] and report["live_acceptance_passed"] is False
    assert report["preserved_data_unchanged"]
    assert report["agent_calls"] == report["embedding_calls"] == 0
    assert not report["background_worker_started"] and report["delivery_attempted"] == 1
    assert report["audit"]["consistent_at_observation"]
    assert path.read_bytes() == raw
    assert journey.store.memory.list_active(UUID(original["rooms"]["personal"])) == ()
    backup = json.loads(Path(report["backup_manifest"]).read_text())
    assert database_snapshot(Path(backup["backup"])) == original["database_snapshot"]
    assert len(remote.points) == 1 and all(m.record is None for m in remote.points.values())
    assert not any(report["outbox_status"].get(k, 0) for k in ("pending", "processing", "waiting", "dead"))
    assert (directory / "report.json").stat().st_mode & 0o777 == 0o600
    # Safe refusal on a second application; no replay/automatic restore.
    with pytest.raises(ValueError, match="reviewed_database_changed"):
        recovery.repair(journey.target)


@pytest.mark.parametrize("change", ["report", "memory", "backlog"])
def test_changed_state_refused_before_backup_or_remote(tmp_path, monkeypatch, change):
    journey, _, path, _ = fixture(tmp_path, monkeypatch)
    if change == "report":
        path.write_text("{}")
    else:
        with journey.database.transaction() as conn:
            if change == "memory":
                conn.execute("UPDATE standalone_chat_memories SET content='changed'")
            else:
                conn.execute("UPDATE standalone_chat_vector_outbox SET status='processing'")
    monkeypatch.setattr(recovery, "backup_database", lambda *a, **k: pytest.fail("backup allocated"))
    monkeypatch.setattr(recovery, "check_vector_worker_target", lambda *a, **k: pytest.fail("remote checked"))
    with pytest.raises(ValueError):
        recovery.repair(journey.target)


def test_backup_failure_stops_before_retirement(tmp_path, monkeypatch):
    journey, _, _, _ = fixture(tmp_path, monkeypatch)
    before = database_snapshot(journey.target.database)
    monkeypatch.setattr(recovery, "backup_database", lambda *a, **k: (tmp_path, {"complete": False}))
    _, report = recovery.repair(journey.target)
    assert not report["complete"] and report["phase"] == "preflight_passed"
    assert database_snapshot(journey.target.database) == before


def test_atomic_retirement_guard_refuses_post_backup_drift(tmp_path, monkeypatch):
    journey, _, _, _ = fixture(tmp_path, monkeypatch)
    expected = database_snapshot(journey.target.database)
    database = recovery.GuardedDatabase(journey.target.database, expected)
    with journey.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_vector_outbox SET attempts=1")
    with pytest.raises(ValueError, match="database_changed_before_retirement"), database.transaction():
        pytest.fail("guard yielded transaction")
