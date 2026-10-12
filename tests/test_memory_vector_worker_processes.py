"""Harness ownership/validity only; fake processes never count as OS evidence."""

import json
import signal
import subprocess
from types import SimpleNamespace

import pytest

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.vector_contracts import VectorModel
from app.storage import SQLiteDatabase
from evals import check_memory_vector_worker_processes as probe
from evals import memory_vector_worker_child as child_module


class Process:
    pid = 123456
    def __init__(self, *, timeout=False):
        self.returncode, self.calls, self.timeout = None, [], timeout
    def poll(self):
        return self.returncode
    def send_signal(self, signum):
        self.calls.append(signum)
        self.returncode = -signum if signum == signal.SIGKILL else 0
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
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        if self.returncode < 0:
            return "", ""
        return json.dumps({"reason": "stopped", "attempted": 0, "clean_shutdown": True,
                           "diagnostics": None, "diagnostics_status": "not_observed", "embedding_calls": 0, "agent_calls": 0}), ""


def fixture(tmp_path):
    journey = SimpleNamespace(database=SQLiteDatabase(tmp_path/"chat.sqlite3"),
        backend=SimpleNamespace(model=VectorModel("synthetic-process-model", 3)))
    endpoint = QdrantEndpoint(base_url="http://127.0.0.1:45678", collection_prefix="codecrew_worker_test")
    return journey, endpoint


def owned(tmp_path, *, process=None, **kwargs):
    journey, endpoint = fixture(tmp_path)
    process = process or Process()
    calls = []
    def launch(*args, **options):
        calls.append((args, options))
        return process
    child = probe.OwnedChild(tmp_path/"child", journey, endpoint, launcher=launch, **kwargs)
    return child, process, calls


def test_plan_opens_no_docker_settings_or_process(monkeypatch, capsys):
    monkeypatch.setattr(probe, "run_suite", lambda **k: pytest.fail("plan ran"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


def test_clean_env_not_inherited_and_owned_cwd_no_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "synthetic-private")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_API_KEY", "synthetic-private")
    child, process, calls = owned(tmp_path)
    child.start()
    args, options = calls[0]
    assert "shell" not in options and options["start_new_session"] is True
    assert options["cwd"] == child.directory
    assert options["env"]["CODECREW_MEMORY_VECTOR_WORKER_ENABLED"] == "true"
    assert "synthetic-private" not in json.dumps(options["env"])
    assert "KIMI_MODEL_API_KEY" not in options["env"] and "HOME" not in options["env"]
    assert "evals.memory_vector_worker_child" in args[0]
    child.send(signal.SIGKILL)
    assert child.reap()["returncode"] == -signal.SIGKILL and process.calls == [signal.SIGKILL]


@pytest.mark.parametrize("url", ["http://127.0.0.1:6333", "http://localhost:45678", "https://example.com", "http://127.0.0.1:70000"])
def test_nonisolated_endpoint_refused_before_launch(tmp_path, url):
    journey, _ = fixture(tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        endpoint = QdrantEndpoint(base_url=url, collection_prefix="codecrew_worker_test")
        probe.OwnedChild(tmp_path/"child", journey, endpoint, launcher=lambda *a, **k: pytest.fail("bad endpoint launch"))


def test_marker_and_release_require_same_nonce(tmp_path):
    child, _, _ = owned(tmp_path, mode="after_remote")
    path = child.directory/(child.token+"-gate.json")
    path.write_text(json.dumps({"token": "b"*32, "phase": "gate"}))
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        child.release()
    path.write_text(json.dumps({"token": child.token, "phase": "gate"}))
    child.release()
    assert json.loads((child.directory/(child.token+"-release.json")).read_text())["token"] == child.token


def test_live_child_handle_only_and_no_signal_after_reap(tmp_path):
    child, process, _ = owned(tmp_path)
    child.start()
    child.send(signal.SIGTERM)
    first = child.reap()
    assert first["report"]["reason"] == "stopped" and child.reap() is first
    with pytest.raises(RuntimeError):
        child.send(signal.SIGKILL)
    assert process.calls == [signal.SIGTERM]


def test_cleanup_bounded_terminate_then_kill_only_owned_handle(tmp_path):
    child, process, _ = owned(tmp_path, process=Process(timeout=True))
    child.start()
    assert child.cleanup() and process.calls == ["terminate", "kill"]
    assert child.cleanup() and process.calls == ["terminate", "kill"]


def test_private_env_in_cwd_refuses_launch(tmp_path):
    child, _, calls = owned(tmp_path)
    (child.directory/".env").write_text("synthetic-private")
    with pytest.raises(RuntimeError):
        child.start()
    assert calls == []


def test_cleanup_attempts_every_child_on_first_failure(tmp_path):
    journey = probe.ProcessJourney(tmp_path, None)
    called = []
    class Broken:
        def cleanup(self):
            called.append("broken")
            raise RuntimeError("synthetic")
    class Good:
        def cleanup(self):
            called.append("good")
            return True
    journey.children = [Broken(), Good()]
    assert not journey.cleanup() and called == ["broken", "good"]


def test_child_stop_marker_is_supervisor_not_signal_handler(tmp_path, monkeypatch):
    spec = {"directory": str(tmp_path), "token": "a"*32, "mode": "none"}
    # Register original values so monkeypatch will restore eval-only replacements.
    for obj, name in ((child_module.vector_worker, "check_vector_worker_target"),
                      (child_module.vector_sync, "VectorWorkerStop"),
                      (child_module.QdrantVersionedVectorBackend, "apply_mutation")):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    child_module.install_hooks(spec)
    stop = child_module.vector_sync.VectorWorkerStop()
    stop.request_stop()
    assert not (tmp_path/(spec["token"]+"-stop.json")).exists()
    stop.observe(2)
    assert json.loads((tmp_path/(spec["token"]+"-stop.json")).read_text())["phase"] == "stop"


def test_failed_lifecycle_always_cleanups_owned_children_and_docker(tmp_path, monkeypatch):
    called = []
    class Docker:
        def __init__(self, path):
            pass
        def create(self):
            raise RuntimeError("synthetic-private")
        def cleanup(self):
            called.append("docker")
            return True
    class Journey:
        def __init__(self, *a):
            self.checks = []
        def cleanup(self):
            called.append("children")
            return True
    monkeypatch.setattr(probe, "OwnedDocker", Docker)
    monkeypatch.setattr(probe, "ProcessJourney", Journey)
    output, report = probe.run_suite(output_root=tmp_path)
    assert not report["complete"] and report["cleanup_complete"]
    assert called == ["children", "docker"] and "synthetic-private" not in (output/"report.json").read_text()


def test_incomplete_cli_is_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "run_suite", lambda: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as caught:
        probe.main(["--live"])
    assert caught.value.code == 1
