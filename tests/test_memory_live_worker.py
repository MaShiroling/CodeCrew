"""Local launcher controls: fake OS processes are NOT actual signal evidence."""

import json
import signal
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest

from app.chat.vector_memory import OpenAICompatibleEmbedder
from app.config import Settings
from scripts import run_memory_live_worker as probe
from tests.test_memory_live_target import Remote, prepare


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("cloud embedding"))


def target_fixture(tmp_path, *, watch=False):
    directory, report, _, _ = prepare(tmp_path)
    calls = []
    def settings_factory(**kwargs):
        calls.append(dict(kwargs))
        kwargs.pop("_env_file")
        return Settings(_env_file=None, memory_embedding_api_key="private-unused-cloud",
                        memory_qdrant_api_key="private-qdrant", **kwargs)
    target = probe.load_target(directory / "prepare.json", target_root=directory.parent,
        settings_factory=settings_factory, backend_factory=lambda e, m: Remote(e, m, existing=True), watch=watch)
    return target, report, calls


def test_plan_opens_nothing(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_target", lambda *a, **k: pytest.fail("opened target"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


def test_manifest_isolation_and_explicit_per_process_enable(tmp_path):
    target, report, calls = target_fixture(tmp_path, watch=True)
    assert target.settings.memory_embedding_mode == "off"
    assert target.settings.memory_vector_worker_enabled is True
    assert target.settings.database_url == report["runtime_overrides"]["database_url"]
    assert target.backend.sync_target_id == report["target_id"]
    assert target.settings.memory_vector_worker_poll_interval_seconds == 2
    assert target.settings.memory_vector_worker_shutdown_grace_seconds == 10
    assert calls[0]["_env_file"] == probe.ROOT / ".env"
    assert json.loads(target.manifest.read_text())["runtime_overrides"]["memory_vector_worker_enabled"] is False


@pytest.mark.parametrize("mutation", ["old_db", "extra_key", "worker_flag", "namespace", "target_id", "collection", "phase", "missing_db", "symlink_db"])
def test_manifest_tampering_refused_without_initialization(tmp_path, mutation):
    directory, data, _, _ = prepare(tmp_path)
    path = directory / "prepare.json"
    values = data["runtime_overrides"]
    if mutation == "old_db":
        data["database"] = str(tmp_path / "old.sqlite3")
    elif mutation == "extra_key":
        values["memory_embedding_api_key"] = "private"
    elif mutation == "worker_flag":
        values["memory_vector_worker_enabled"] = True
    elif mutation == "namespace":
        values["memory_qdrant_collection_prefix"] = "codecrew_memory"
    elif mutation in {"target_id", "collection", "phase"}:
        data[mutation] = "wrong"
    elif mutation == "missing_db":
        # Test-owned empty temp file only; production launcher never removes DBs.
        (directory / "chat.sqlite3").rename(directory / "retained.sqlite3")
    else:
        (directory / "chat.sqlite3").rename(directory / "retained.sqlite3")
        (directory / "chat.sqlite3").symlink_to(directory / "retained.sqlite3")
    path.write_text(json.dumps(data))
    def safe_settings(**kwargs):
        kwargs.pop("_env_file")
        return Settings(_env_file=None, **kwargs)
    with pytest.raises((ValueError, FileNotFoundError)):
        probe.load_target(path, target_root=directory.parent, settings_factory=safe_settings,
                          backend_factory=lambda e, m: Remote(e, m, existing=True))


def test_manifest_outside_selected_root_refused(tmp_path):
    target, _, _ = target_fixture(tmp_path)
    with pytest.raises(ValueError, match="private_target"):
        probe.load_target(target.manifest, target_root=tmp_path / "wrong", settings_factory=lambda **k: pytest.fail("settings opened"))


def test_database_diagnostics_not_heartbeat_and_no_migration(tmp_path):
    target, _, _ = target_fixture(tmp_path)
    before = probe.inventory(target.database)
    frame = probe.diagnostics(target)
    assert frame["target_registered"] and frame["backlog_total"] == 0
    assert frame["last_delivered_at_unix"] is None
    assert probe.inventory(target.database) == before


def test_clean_environment_does_not_copy_keys_or_target(monkeypatch):
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_API_KEY", "private")
    monkeypatch.setenv("CODECREW_DATABASE_URL", "wrong")
    env = probe.child_environment()
    assert set(env) == {"PATH", "PYTHONPATH", "PYTHONUNBUFFERED"}
    assert "private" not in json.dumps(env) and "HOME" not in env and "CODEX_HOME" not in env


class Process:
    next_pid = 123000
    def __init__(self, *, stdout=None, target=None, timeout=False):
        Process.next_pid += 1
        self.pid, self.returncode, self.calls = Process.next_pid, None, []
        self.stdout, self.target, self.timeout = stdout, target, timeout
        if stdout:
            stdout.write(json.dumps({"event": "preflight_passed", "pid": self.pid, "target_id": target.backend.sync_target_id}) + "\n")
            stdout.flush()
    def poll(self):
        return self.returncode
    def send_signal(self, signum):
        self.calls.append(signum)
        self.returncode = 0
        if self.stdout:
            self.stdout.write(json.dumps({"event": "watch_exited", "reason": "stopped", "attempted": 0, "clean_shutdown": True,
                "diagnostics": probe.diagnostics(self.target), "diagnostics_status": "observed", "embedding_calls": 0, "agent_calls": 0}) + "\n")
            self.stdout.flush()
    def terminate(self):
        self.calls.append("terminate")
        if not self.timeout:
            self.returncode = 0
    def kill(self):
        self.calls.append("kill")
        self.returncode = -signal.SIGKILL
    def wait(self, *, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        return self.returncode
    def communicate(self, *, timeout):
        return None, None


def test_cleanup_direct_handle_bounded_terminate_kill_reap():
    proc = Process(timeout=True)
    assert probe.cleanup_child(proc) and proc.calls == ["terminate", "kill"]
    assert probe.cleanup_child(proc) and proc.calls == ["terminate", "kill"]


def test_fake_driver_exercises_two_signals_and_no_shell_or_env_copy(tmp_path, monkeypatch):
    target, _, _ = target_fixture(tmp_path)
    launched = []
    def launch(args, **options):
        assert "--watch" in args and str(target.manifest) in args
        assert "shell" not in options and options["start_new_session"]
        assert set(options["env"]) == {"PATH", "PYTHONPATH", "PYTHONUNBUFFERED"}
        proc = Process(stdout=options["stdout"], target=target)
        launched.append(proc)
        return proc
    # Synthetic time for harness control tests only; live driver uses real clock.
    tick = [0.]
    def clock():
        tick[0] += 10
        return tick[0]
    monkeypatch.setattr(probe.time, "monotonic", clock)
    output, report = probe.check_target(target, launcher=launch)
    assert report["complete"] and report["children_cleanup_complete"] and not report["worker_running_after_check"]
    assert [p.calls for p in launched] == [[signal.SIGTERM], [signal.SIGINT]]
    assert report["inventory_after"] == report["inventory_before"]
    assert "private-unused-cloud" not in (output / "report.json").read_text()
    assert "private-qdrant" not in (output / "report.json").read_text()


def test_watch_reuses_runtime_and_signal_restore_without_provider(tmp_path, monkeypatch, capsys):
    target, _, _ = target_fixture(tmp_path, watch=True)
    seen = []
    original = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(probe, "watch_vector_worker", lambda database, backend, policy, **kwargs:
        (seen.append((database, backend, policy)) or SimpleNamespace(exit_code=0, reason="stopped", attempted=0,
             diagnostics={"backlog_total": 0}, diagnostics_status="observed")))
    assert probe.watch_target(target) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in events] == ["preflight_passed", "watch_exited"]
    assert events[-1]["agent_calls"] == events[-1]["embedding_calls"] == 0
    assert seen[0][0] == target.database
    assert {s: signal.getsignal(s) for s in original} == original


def test_check_refuses_nonempty_before_launch(tmp_path):
    target, _, _ = target_fixture(tmp_path)
    from uuid import uuid4

    from app.chat.service import StandaloneChatService
    from app.chat.store import StandaloneChatStore
    from app.storage import SQLiteDatabase
    store = StandaloneChatStore(SQLiteDatabase(target.database))
    service = StandaloneChatService(store)
    room = service.create_room(title="unit", idempotency_key=uuid4()).room_id
    source = service.post_message(room, content="@白金 synthetic", idempotency_key=uuid4(), reply_to=None).message
    service.create_memory(room, source_message_id=source.message_id, content="synthetic")
    with pytest.raises(ValueError, match="empty_target"):
        probe.check_target(target, launcher=lambda *a, **k: pytest.fail("nonempty launched"))


def test_early_child_failure_reports_incomplete_and_reaps_handle(tmp_path, monkeypatch):
    target, _, _ = target_fixture(tmp_path)
    proc = Process()
    proc.returncode = 1
    launched = []
    def launch(*args, **kwargs):
        launched.append(proc)
        return proc
    monkeypatch.setattr(probe, "wait_until", lambda predicate, **k: predicate())
    output, report = probe.check_target(target, launcher=launch)
    assert not report["complete"] and report["error_type"] == "ValueError"
    assert report["children_cleanup_complete"] and not report["worker_running_after_check"]
    assert launched == [proc] and proc.calls == []
    assert (output / "report.json").exists()
