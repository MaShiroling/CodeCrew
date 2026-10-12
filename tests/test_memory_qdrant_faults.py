"""Fault journeys use real SQLite/FTS/outbox/index, fake remote/controls only."""

import copy
import json
import urllib.request
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.vector_contracts import VectorBackendError, VectorHit, VectorModel
from app.chat.vector_maintenance_contracts import (
    RemoteIdentity,
    RemotePage,
    VectorInspection,
    cursor_key,
)
from app.chat.vector_memory import _cosine
from evals import check_memory_qdrant_faults as faults


class OfflineStorage:
    model = VectorModel(faults.SyntheticEmbedder.model_fingerprint, 3)
    sync_target_id = "d" * 64

    def __init__(self):
        self.points, self.running, self.stops, self.starts = {}, True, 0, 0

    def stop(self):
        self.running = False
        self.stops += 1

    def start(self):
        self.running = True
        self.starts += 1

    def _require_sync_ready(self):
        if not self.running:
            raise VectorBackendError("synthetic outage")

    @staticmethod
    def point_id(mutation):
        return str(uuid5(NAMESPACE_URL, f"codecrew:vector:v1:{mutation.room_id}:{mutation.memory_id}"))

    def apply_mutation(self, mutation):
        self._require_sync_ready()
        key = self.point_id(mutation)
        current = self.points.get(key)
        if current is None or current.revision < mutation.revision:
            self.points[key] = mutation
        return self.points[key].revision

    def search_current(self, request, mutations):
        self._require_sync_ready()
        hits = [VectorHit(m.record.anchor, request.model, _cosine(request.vector, m.record.vector))
                for m in mutations if self.points.get(self.point_id(m)) == m and m.record is not None
                and _cosine(request.vector, m.record.vector) >= request.min_similarity]
        return tuple(sorted(hits, key=lambda h: (-h.similarity, str(h.anchor.memory_id))))

    def inspect_mutations(self, mutations):
        self._require_sync_ready()
        result = []
        for m in mutations:
            existing = self.points.get(self.point_id(m))
            code = "missing" if existing is None else "current" if existing == m else "drift"
            result.append(VectorInspection(m.memory_id, code, existing.revision if existing else None))
        return tuple(result)

    def scan_identities(self, cursor=None, *, limit=16):
        self._require_sync_ready()
        items = sorted([RemoteIdentity(key, m.room_id, m.memory_id) for key, m in self.points.items()],
                       key=lambda item: cursor_key(item.point_id))
        selected = [i for i in items if cursor is None or cursor_key(i.point_id) >= cursor_key(cursor)]
        return RemotePage(tuple(selected[:limit]), selected[limit].point_id if len(selected) > limit else None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))


@pytest.mark.parametrize("lexical", ["legacy", "bm25"])
def test_entire_fault_lifecycle_matrix_offline(tmp_path, lexical):
    remote = OfflineStorage()
    journey = faults.FaultJourney(tmp_path / "isolated.sqlite3", remote, QdrantEndpoint(), remote,
                                 lexical_backend=lexical)
    report = journey.run()
    assert len(report["checks"]) == 9
    assert remote.stops == remote.starts == 6
    assert report["outage_status"]["dead"] == 3
    assert report["final_audit"]["consistent_at_observation"]
    assert not any(report["outbox_status"].get(s) for s in ("pending", "processing", "waiting", "dead"))
    assert report["synthetic_query_vector_requests"] > 0
    assert report["worker_crash_scope"].endswith("no OS kill")


def test_remote_data_loss_is_not_claimed_as_success(tmp_path):
    remote = OfflineStorage()
    original_start = remote.start
    def lose_storage():
        remote.points.clear()
        original_start()
    remote.start = lose_storage
    journey = faults.FaultJourney(tmp_path / "isolated.sqlite3", remote, QdrantEndpoint(), remote)
    with pytest.raises(RuntimeError, match="remote_data_not_persistent"):
        journey.run()
    assert len(journey.checks) == 1


class DockerRunner:
    def __init__(self):
        self.calls, self.info, self.fail_inspect, self.daemon_down = [], None, False, False

    def __call__(self, command, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "timeout": 30, "check": False}
        self.calls.append(command[1:])
        args = command[1:]
        if args[:2] == ["container", "inspect"]:
            return SimpleNamespace(returncode=1 if self.info is None or self.fail_inspect else 0,
                                   stdout="" if self.info is None else json.dumps([self.info]))
        if args[0] == "info":
            return SimpleNamespace(returncode=int(self.daemon_down), stdout="29.8.2")
        if args[:2] == ["container", "ls"]:
            return SimpleNamespace(returncode=0, stdout="" if self.info is None else self.info["Name"][1:])
        if args[0] == "rm":
            self.info = None
        return SimpleNamespace(returncode=0, stdout="")


def owned(tmp_path):
    runner = DockerRunner()
    control = faults.OwnedDocker(tmp_path / "qdrant-storage", runner=runner)
    control.image_id, control.port, control.cid, control.attempted = "sha256:" + "1" * 64, 19001, "a" * 64, True
    runner.info = {"Id": control.cid, "Name": "/" + control.name, "Image": control.image_id,
                   "Config": {"Labels": {faults.LABEL: control.token}},
                   "Mounts": [{"Type": "bind", "Source": str(control.storage), "Destination": "/qdrant/storage"}],
                   "HostConfig": {"PortBindings": {"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19001"}]}},
                   "State": {"Running": True}}
    return control, runner


@pytest.mark.parametrize("field", ["id", "name", "label", "image", "mount_source", "mount_target",
                                  "mount_type", "extra_mount", "public_port", "host_port"])
@pytest.mark.parametrize("action", ["stop", "cleanup"])
def test_foreign_or_changed_container_is_never_mutated(tmp_path, field, action):
    control, runner = owned(tmp_path)
    info = runner.info
    if field == "id":
        info["Id"] = "b" * 64
    elif field == "name":
        info["Name"] = "/shared-qdrant"
    elif field == "label":
        info["Config"]["Labels"][faults.LABEL] = "someone-else"
    elif field == "image":
        info["Image"] = "sha256:" + "2" * 64
    elif field == "mount_source":
        info["Mounts"][0]["Source"] = "/shared-data"
    elif field == "mount_target":
        info["Mounts"][0]["Destination"] = "/other"
    elif field == "mount_type":
        info["Mounts"][0]["Type"] = "volume"
    elif field == "extra_mount":
        info["Mounts"].append(copy.deepcopy(info["Mounts"][0]))
    elif field == "public_port":
        info["HostConfig"]["PortBindings"]["6333/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        info["HostConfig"]["PortBindings"]["6333/tcp"][0]["HostPort"] = "6333"
    with pytest.raises(RuntimeError):
        getattr(control, action)()
    assert not any(call[0] in ("stop", "start", "rm", "restart") for call in runner.calls)


def test_cleanup_removes_only_owned_id_and_keeps_bind_data(tmp_path):
    control, runner = owned(tmp_path)
    control.storage.mkdir()
    assert control.cleanup()
    assert [call for call in runner.calls if call[0] == "rm"] == [["rm", "--force", control.cid]]
    assert control.storage.exists()
    assert not any("prune" in call or "--volumes" in call for call in runner.calls)


def test_partial_creation_resolves_name_but_still_checks_ownership(tmp_path):
    control, runner = owned(tmp_path)
    control.cid = None
    expected = runner.info["Id"]
    assert control.cleanup() and control.cid == expected


@pytest.mark.parametrize("down", [False, True])
def test_inspection_or_daemon_failure_does_not_claim_cleanup(tmp_path, down):
    control, runner = owned(tmp_path)
    runner.fail_inspect, runner.daemon_down = True, down
    with pytest.raises(RuntimeError):
        control.cleanup()
    assert not any(call[0] == "rm" for call in runner.calls)


def test_no_attempt_means_no_cleanup_commands(tmp_path):
    runner = DockerRunner()
    control = faults.OwnedDocker(tmp_path / "qdrant-storage", runner=runner)
    assert control.cleanup() and runner.calls == []


def test_default_plan_has_zero_side_effects(monkeypatch, capsys):
    monkeypatch.setattr(faults, "probe", lambda **kwargs: pytest.fail("plan ran live"))
    faults.main([])
    assert "No image pull" in capsys.readouterr().out


@pytest.mark.parametrize("collision", [False, True])
def test_create_pins_local_image_forbids_pull_and_rejects_name_collision(tmp_path, monkeypatch, collision):
    control, runner = owned(tmp_path)
    info = copy.deepcopy(runner.info)
    control.cid, control.attempted = None, False
    if not collision:
        runner.info = None
    def run(command, **kwargs):
        if command[1:3] == ["image", "inspect"]:
            runner.calls.append(command[1:])
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": control.image_id}]))
        if command[1] == "create":
            runner.calls.append(command[1:])
            runner.info = info
            return SimpleNamespace(returncode=0, stdout=info["Id"])
        return runner(command, **kwargs)
    control.runner = run
    class BoundSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, address):
            assert address == ("127.0.0.1", 0)
        def getsockname(self):
            return "127.0.0.1", 19001
    monkeypatch.setattr(faults.socket, "socket", BoundSocket)
    if collision:
        with pytest.raises(RuntimeError, match="docker_name_collision"):
            control.create()
        assert not control.attempted and control.cleanup()
        assert not any(call[0] in ("create", "stop", "start", "rm") for call in runner.calls)
    else:
        control.create()
        create = next(call for call in runner.calls if call[0] == "create")
        assert "--pull=never" in create and create[-1] == control.image_id
        assert create[create.index("--publish") + 1] == "127.0.0.1:19001:6333"
        assert control.cleanup()


@pytest.mark.parametrize("failure", ["create", "start", "initialize", "journey", "cleanup", None])
def test_probe_always_attempts_owned_cleanup_and_redacts_errors(tmp_path, monkeypatch, failure):
    calls = []
    def trigger(stage):
        calls.append(stage)
        if failure == stage:
            raise RuntimeError("private-provider-response-and-secret")
    class Control:
        name, image_id, url = "synthetic-owned", "sha256:synthetic", "http://127.0.0.1:19001"
        def __init__(self, storage):
            pass
        def create(self):
            trigger("create")
        def start(self):
            trigger("start")
        def cleanup(self):
            trigger("cleanup")
            return True
    class Backend:
        def __init__(self, *args):
            pass
        def initialize(self):
            trigger("initialize")
    class Journey:
        def __init__(self, *args):
            self.checks = []
            self.embedder = SimpleNamespace(calls=0)
        def run(self):
            trigger("journey")
            return {}
    monkeypatch.setattr(faults, "OwnedDocker", Control)
    monkeypatch.setattr(faults, "QdrantVersionedVectorBackend", Backend)
    monkeypatch.setattr(faults, "FaultJourney", Journey)
    output, report = faults.probe(output_root=tmp_path)
    assert calls[-1] == "cleanup"
    assert report["complete"] is (failure is None)
    assert report["cleanup_complete"] is (failure != "cleanup")
    assert report["embedding_calls"] == report["agent_calls"] == 0
    assert "private-provider" not in (output / "report.json").read_text()


def test_cli_fails_when_probe_or_cleanup_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(faults, "probe", lambda: (tmp_path, {"complete": False}))
    with pytest.raises(SystemExit) as exc:
        faults.main(["--live"])
    assert exc.value.code == 1
