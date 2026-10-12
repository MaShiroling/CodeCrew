"""Read-only restart controls; fake launcher/remote are not real OS evidence."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.chat.vector_maintenance_contracts import RemoteIdentity, RemotePage
from app.chat.vector_memory import OpenAICompatibleEmbedder
from evals import check_memory_persistent_restart as restart
from evals import run_memory_persistent_followup as followup
from evals.memory_persistent_resume import database_snapshot
from scripts import recover_memory_live_miswrite as recovery
from tests.test_memory_persistent_followup import adapter, fixture


@pytest.fixture(autouse=True)
def no_paid_or_network(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("cloud embedding"))
    monkeypatch.setattr(followup.KimiCodeAdapter, "start", lambda *a, **k: pytest.fail("paid Agent"))


async def ready(tmp_path, monkeypatch):
    journey, remote, recovery_path, state = await fixture(tmp_path, monkeypatch)
    # Complete the synthetic historical artifact; never patch real evidence.
    parent = state["parent_path"]
    original = json.loads(parent.read_text())
    original["write_proposals"] = journey.proposals
    parent.write_text(json.dumps(original))
    monkeypatch.setattr(recovery, "REVIEWED_SHA", hashlib.sha256(parent.read_bytes()).hexdigest())
    recovered = json.loads(recovery_path.read_text())
    recovered["parent_report_sha256"] = recovery.REVIEWED_SHA
    recovery_path.write_text(json.dumps(recovered))
    state = followup.inspect_recovery(journey.target, recovery_path)
    directory, report = await followup.run_cases(journey.target, state, adapter(), tmp_path / "execution")
    assert report["complete"]
    path = directory / "report.json"
    monkeypatch.setattr(restart, "REVIEWED_FOLLOWUP_SHA", hashlib.sha256(path.read_bytes()).hexdigest())
    return journey, remote, path, restart.inspect_followup(journey.target, path)


def test_plan_opens_nothing(monkeypatch, capsys):
    monkeypatch.setattr(restart, "load_target", lambda *a: pytest.fail("opened target"))
    restart.main([])
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--run"], ["--check"], ["--nonce", "a" * 32], ["--check", "--run"]])
def test_invalid_cli_no_target(monkeypatch, args):
    monkeypatch.setattr(restart, "load_target", lambda *a: pytest.fail("opened target"))
    with pytest.raises(SystemExit):
        restart.main(args)


def test_readonly_database_rejects_sql_writes_transactions_and_missing_files(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE data(value)")
    database = restart.ReadOnlyDatabase(path)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM data").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO data VALUES(1)")
    with pytest.raises(RuntimeError, match="write_forbidden"), database.transaction():
        pytest.fail("write transaction opened")
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError), restart.ReadOnlyDatabase(missing).connect():
        pytest.fail("missing database created")
    assert not missing.exists()


@pytest.mark.asyncio
async def test_reopen_checks_all_five_records_sources_and_derivatives_without_writes(tmp_path, monkeypatch):
    journey, _, _, state = await ready(tmp_path, monkeypatch)
    result = restart.observe(journey.target, state)
    assert result["active_count"] == 1 and result["retired_count"] == 4
    assert result["remote_current"] == result["remote_scanned"] == 5 and result["remote_tombstones"] == 4
    assert result["cached_vector_replays"] == 2 and result["cached_vector_self_match"]
    assert result["cancelled_receipt_preserved"] and result["vector_room_and_discussion_isolation"]
    assert database_snapshot(journey.target.database) == state["before"]
    assert restart.evidence_unchanged(state)


@pytest.mark.parametrize("change", ["database", "code", "parent", "recovery", "followup", "outside"])
@pytest.mark.asyncio
async def test_preflight_rejects_drift_before_subprocess_or_report(tmp_path, monkeypatch, change):
    journey, _, path, _ = await ready(tmp_path, monkeypatch)
    if change == "database":
        with journey.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_memories SET content='changed' WHERE memory_id=?", (str(recovery.BAD_ID),))
    elif change == "code":
        monkeypatch.setattr(followup, "fingerprints", lambda: {"changed": "changed"})
    elif change == "outside":
        other = tmp_path / "report.json"
        other.write_bytes(path.read_bytes())
        path = other
    else:
        report = json.loads(path.read_text())
        changed = path if change == "followup" else Path(report[change + "_report"])
        changed.write_text("{}")
    with pytest.raises(ValueError):
        restart.run_restart(journey.target, path, launcher=lambda *a, **k: pytest.fail("launched child"))
    assert not (journey.target.directory / "restart-runs").exists()


@pytest.mark.parametrize("change", ["retired_active", "cache", "fts", "closure", "pending"])
@pytest.mark.asyncio
async def test_canonical_checks_fail_on_resurrection_or_corruption(tmp_path, monkeypatch, change):
    journey, _, _, state = await ready(tmp_path, monkeypatch)
    with journey.database.transaction() as conn:
        if change == "retired_active":
            conn.execute("UPDATE standalone_chat_memories SET status='active' WHERE memory_id=?", (str(recovery.BAD_ID),))
        elif change == "cache":
            conn.execute("DELETE FROM standalone_chat_memory_embeddings")
        elif change == "fts":
            conn.execute("UPDATE standalone_chat_memory_fts_state SET status='dirty'")
        elif change == "closure":
            conn.execute("UPDATE standalone_chat_memories SET closed_by_message_id=NULL WHERE open_loop_state='cancelled'")
        else:
            conn.execute("UPDATE standalone_chat_vector_outbox SET status='pending' WHERE event_id=(SELECT event_id FROM standalone_chat_vector_outbox LIMIT 1)")
    with pytest.raises(ValueError):
        restart.canonical_checks(journey.target, state)


@pytest.mark.parametrize("change", ["missing", "stale_tombstone", "orphan", "incomplete", "down"])
@pytest.mark.asyncio
async def test_remote_audit_failure_not_passed_or_repaired(tmp_path, monkeypatch, change):
    journey, remote, _, state = await ready(tmp_path, monkeypatch)
    if change == "missing":
        remote.points.pop(next(iter(remote.points)))
    elif change == "stale_tombstone":
        key = next(k for k, m in remote.points.items() if m.record is None)
        remote.points[key] = replace(remote.points[key], revision=remote.points[key].revision + 1)
    elif change == "orphan":
        original_scan = remote.scan_identities
        def scan(cursor=None, *, limit=16):
            page = original_scan(cursor, limit=limit)
            points = (*page.points, RemoteIdentity(str(uuid4())))
            return RemotePage(tuple(sorted(points, key=lambda p: p.point_id)), None)
        monkeypatch.setattr(remote, "scan_identities", scan)
    elif change == "incomplete":
        monkeypatch.setattr(remote, "scan_identities", lambda *a, **k: RemotePage((), None))
    else:
        remote.running = False
    before = dict(remote.points)
    with pytest.raises((ValueError, RuntimeError)):
        restart.observe(journey.target, state)
    assert remote.points == before and database_snapshot(journey.target.database) == state["before"]


class Launcher:
    def __init__(self, target, state, *, change=None):
        self.target, self.state, self.change, self.processes = target, state, change, []

    def __call__(self, command, **kwargs):
        assert command[:3] == [sys.executable, "-m", "evals.check_memory_persistent_restart"]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["start_new_session"]
        assert set(kwargs["env"]) == {"PATH", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}
        assert "KIMI_MODEL_API_KEY" not in kwargs["env"] and "HOME" not in kwargs["env"]
        pid = os.getpid() + 100 + len(self.processes)
        report = {"complete": True, "pid": pid, "nonce": command[-1], "database": str(self.target.database),
            "target_id": self.target.backend.sync_target_id, "before": self.state["before"], "after": self.state["before"],
            "code_sha256": restart.fingerprints(), "evidence_sha256": self.state["evidence"],
            "agent_calls": 0, "embedding_api_calls": 0, "checks": restart.observe(self.target, self.state)}
        if self.change == "identity":
            report["pid"] += 1
        elif self.change == "child_failure":
            report["complete"] = False
        elif self.change == "different" and self.processes:
            report["checks"]["remote_current"] = 0
        elif self.change == "stderr":
            kwargs["stderr"].write("private body must not be echoed")
        kwargs["stdout"].write(json.dumps(report))
        proc = FakeProcess(pid, self.change)
        self.processes.append(proc)
        return proc


class FakeProcess:
    def __init__(self, pid, change):
        self.pid, self.returncode, self.change = pid, None, change
    def wait(self, *, timeout):
        if self.change == "timeout" and self.returncode is None:
            raise subprocess.TimeoutExpired("test-owned", timeout)
        self.returncode = 0
        return 0
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = -15
    def kill(self):
        self.returncode = -9
    def communicate(self, *, timeout):
        return "", ""


@pytest.mark.asyncio
async def test_two_owned_readers_success_private_evidence_and_unchanged_state(tmp_path, monkeypatch):
    journey, _, path, state = await ready(tmp_path, monkeypatch)
    launcher = Launcher(journey.target, state)
    output, report = restart.run_restart(journey.target, path, launcher=launcher)
    assert report["complete"] and report["restart_passed"] and report["children_cleanup_complete"]
    assert len({p["pid"] for p in report["processes"]}) == 2 and all(p.poll() == 0 for p in launcher.processes)
    assert report["agent_calls"] == report["embedding_api_calls"] == 0 and report["final_state_unchanged"]
    assert report["before"] == report["after"] == state["before"] and restart.evidence_unchanged(state)
    assert report["cumulative_kimi_starts"] == 13 and report["cumulative_embedding_requests"] == 19
    assert not report["docker_restarted"] and not report["chat_service_restarted"] and not report["background_worker_started"]
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("change", ["identity", "child_failure", "different", "stderr", "timeout"])
@pytest.mark.asyncio
async def test_owned_child_failure_retained_no_repair_retry_or_unclean_success(tmp_path, monkeypatch, change):
    journey, _, path, state = await ready(tmp_path, monkeypatch)
    launcher = Launcher(journey.target, state, change=change)
    output, report = restart.run_restart(journey.target, path, launcher=launcher)
    assert not report["complete"] and not report["restart_passed"] and report["error_type"]
    assert (output / "report.json").exists() and all(p.poll() is not None for p in launcher.processes)
    assert len(launcher.processes) <= 2 and report["before"] == report["after"] == state["before"]
    assert "private body" not in json.dumps(report)
    assert report["children_cleanup_complete"]  # Even a single failed reader is actually reaped.


@pytest.mark.asyncio
async def test_final_cleanup_mutation_cannot_be_reported_as_success(tmp_path, monkeypatch):
    journey, _, path, state = await ready(tmp_path, monkeypatch)
    cleanup, calls = restart.cleanup_child, []
    def changing_cleanup(proc):
        calls.append(proc.pid)
        if len(calls) == 3:  # Both observed readers finished; change only the test-owned DB.
            with journey.database.transaction() as conn:
                conn.execute("UPDATE standalone_chat_messages SET created_at='changed' WHERE sequence=1")
        return cleanup(proc)
    monkeypatch.setattr(restart, "cleanup_child", changing_cleanup)
    _, report = restart.run_restart(journey.target, path, launcher=Launcher(journey.target, state))
    assert report["checks_passed"] and report["children_cleanup_complete"]
    assert not report["final_state_unchanged"] and not report["complete"]


@pytest.mark.asyncio
async def test_cancellation_preserves_incomplete_report_and_never_retries(tmp_path, monkeypatch):
    journey, _, path, state = await ready(tmp_path, monkeypatch)
    def interrupted(*a, **k):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        restart.run_restart(journey.target, path, launcher=interrupted)
    reports = list((journey.target.directory / "restart-runs").glob("*/report.json"))
    assert len(reports) == 1
    record = json.loads(reports[0].read_text())
    assert not record["complete"] and not record["restart_passed"] and record["launched_readers"] == 0
    assert record["after"] == state["before"] and restart.evidence_unchanged(state)


@pytest.mark.asyncio
async def test_cached_replay_cannot_produce_any_new_query_vector(tmp_path, monkeypatch):
    journey, _, _, state = await ready(tmp_path, monkeypatch)
    _, _, _, active, mutations, _, _ = restart.canonical_checks(journey.target, state)
    mutation = next(m for m in mutations if m.record is not None)
    replay = restart.CachedVectorReplay(mutation, active.content)
    with pytest.raises(ValueError, match="new_query_vector_forbidden"):
        replay.embed(["another question"])
    assert replay.calls == 0


@pytest.mark.asyncio
async def test_local_cli_preflight_no_network_process_or_report(tmp_path, monkeypatch, capsys):
    journey, _, path, state = await ready(tmp_path, monkeypatch)
    monkeypatch.setattr(restart, "load_target", lambda *a: journey.target)
    monkeypatch.setattr(restart, "run_restart", lambda *a, **k: pytest.fail("run"))
    monkeypatch.setattr(restart, "check_vector_worker_target", lambda *a: pytest.fail("remote"))
    restart.main(["--check", "--manifest", str(journey.target.manifest), "--followup", str(path)])
    assert "local read-only" in capsys.readouterr().out
    assert not (journey.target.directory / "restart-runs").exists()
    assert database_snapshot(journey.target.database) == state["before"]


def test_actual_python_reopen_is_a_fresh_process_and_readonly(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER)")
    code = "from evals.check_memory_persistent_restart import ReadOnlyDatabase; from pathlib import Path; import os,sys; db=ReadOnlyDatabase(Path(sys.argv[1])); print(os.getpid(),db.schema_version)"
    observations = [subprocess.run([sys.executable, "-c", code, str(path)], env=restart.child_environment(),
        cwd=tmp_path, capture_output=True, text=True, timeout=15, check=True) for _ in range(2)]
    pids = {int(p.stdout.split()[0]) for p in observations}
    assert len(pids) == 2 and os.getpid() not in pids
    assert all(p.stdout.split()[1] == "0" and p.stderr == "" for p in observations)
