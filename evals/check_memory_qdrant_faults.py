"""Opt-in outage/recovery journey in one owned, disposable Docker container.

No settings/.env, real chat DB, cloud embeddings, Agents, image pull or shared
Qdrant service. Retains isolated SQLite/report/bind storage for forensic review.
"""

import argparse
import hashlib
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.chat.dispatch import AgentMemoryProposal
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_outbox import MemoryVectorOutbox, SyncPolicy
from app.storage import SQLiteDatabase
from evals.check_memory_vector_maintenance import finish

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-qdrant-faults-v1"
IMAGE = "qdrant/qdrant:v1.19.0"
LABEL = "codecrew.memory.fault-probe"


def require(condition, check):
    if not condition:
        raise RuntimeError(check)  # Constant diagnostic, never provider text.


class OwnedDocker:
    """Control only a UUID-named, label/image/mount/port-verified container.

    Commands have no shell and bounded timeouts. Creation failures may resolve
    our unique name, but never authorize mutation merely because a name exists.
    """

    def __init__(self, storage, *, runner=subprocess.run):
        self.storage = Path(storage).resolve()
        self.token = uuid4().hex
        self.name = "codecrew-memory-fault-" + self.token
        self.runner = runner
        self.docker = "/Users/mashiro/.docker/bin/docker"
        self.cid = self.image_id = None
        self.attempted = False
        self.port = None

    def command(self, *args, allow_failure=False):
        result = self.runner([self.docker, *args], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode and not allow_failure:
            raise RuntimeError("docker_command_failed")
        return result

    def inspect(self, target):
        result = self.command("container", "inspect", target, allow_failure=True)
        if result.returncode:
            # Daemon unavailable must not be mistaken for successful removal.
            self.command("info", "--format", "{{.ServerVersion}}")
            names = self.command("container", "ls", "--all", "--format", "{{.Names}}").stdout.splitlines()
            require(self.name not in names, "docker_inspect_failed_for_existing_container")
            return None
        rows = json.loads(result.stdout)
        require(isinstance(rows, list) and len(rows) == 1, "docker_inspect_invalid")
        return rows[0]

    def owned(self):
        info = self.inspect(self.cid or self.name)
        if info is None:
            return None
        cid = info.get("Id", "")
        require(bool(re.fullmatch(r"[a-f0-9]{64}", cid)), "docker_identity_invalid")
        require(info.get("Name") == "/" + self.name and
                info.get("Config", {}).get("Labels", {}).get(LABEL) == self.token and
                info.get("Image") == self.image_id and
                (self.cid is None or cid == self.cid), "docker_ownership_mismatch")
        mounts = info.get("Mounts", [])
        require(len(mounts) == 1 and mounts[0].get("Type") == "bind" and
                mounts[0].get("Source") == str(self.storage) and
                mounts[0].get("Destination") == "/qdrant/storage", "docker_mount_mismatch")
        require(info.get("HostConfig", {}).get("PortBindings") == {
            "6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}]}, "docker_port_mismatch")
        self.cid = cid
        return info

    def create(self):
        image = json.loads(self.command("image", "inspect", IMAGE).stdout)
        require(isinstance(image, list) and len(image) == 1, "docker_image_invalid")
        self.image_id = image[0]["Id"]
        require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id)), "docker_image_invalid")
        require(self.inspect(self.name) is None, "docker_name_collision")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.storage.mkdir(exist_ok=False)
        self.attempted = True
        # Image ID pins the local image, --pull=never forbids downloading.
        result = self.command("create", "--pull=never", "--name", self.name,
                              "--label", LABEL + "=" + self.token, "--restart=no",
                              "--publish", f"127.0.0.1:{self.port}:6333",
                              "--mount", f"type=bind,src={self.storage},dst=/qdrant/storage",
                              "--env", "QDRANT__TELEMETRY_DISABLED=true", self.image_id)
        require(bool(re.fullmatch(r"[a-f0-9]{64}", result.stdout.strip())), "docker_create_invalid")
        self.cid = result.stdout.strip()
        self.owned()

    @property
    def url(self):
        require(type(self.port) is int, "docker_not_prepared")
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        require(self.owned() is not None, "docker_owned_container_missing")
        self.command("start", self.cid)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                with opener.open(self.url + "/readyz", timeout=1) as response:
                    if response.status == 200:
                        require(self.owned()["State"]["Running"], "docker_not_running")
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(.2)
        raise RuntimeError("qdrant_readiness_timeout")

    def stop(self):
        require(self.owned() is not None, "docker_owned_container_missing")
        self.command("stop", "--timeout", "5", self.cid)
        require(not self.owned()["State"]["Running"], "docker_not_stopped")

    def cleanup(self):
        if not self.attempted:
            return True
        if self.owned() is not None:
            # Never remove mounts/volumes; preserve forensic bind data.
            self.command("rm", "--force", self.cid)
        require(self.inspect(self.name) is None, "docker_cleanup_incomplete")
        return True


class SyntheticEmbedder:
    model_fingerprint = "codecrew-qdrant-faults-v1"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return tuple((1., 0., 0.) for _ in texts)


class FaultJourney:
    """Same real canonical/outbox/recall journey for offline and live controls."""

    def __init__(self, path, backend, endpoint, control, *, lexical_backend="bm25"):
        self.database = SQLiteDatabase(path)
        self.backend, self.control = backend, control
        self.clock, self.records, self.checks = [100.], {}, []
        self.embedder = SyntheticEmbedder()
        self.index = QdrantMemoryVectorIndex(self.database, self.embedder, endpoint,
                                           backend_factory=lambda model: backend)
        self.store = StandaloneChatStore(self.database, vector_index=self.index, lexical_backend=lexical_backend)
        self.store.initialize()
        if lexical_backend == "bm25":
            require(self.store.memory.fts_index.status()["usable"], "actual_fts5_required")
        self.service = StandaloneChatService(self.store)
        self.room = self.service.create_room(title="synthetic faults", idempotency_key=uuid4()).room_id
        self.other = self.service.create_room(title="synthetic isolation", idempotency_key=uuid4()).room_id
        self.lexical_backend = lexical_backend
        self.policy = SyncPolicy(max_attempts=2)
        self.worker = self.new_worker()
        self.worker.register_target()

    def new_worker(self):
        return MemoryVectorOutbox(SQLiteDatabase(self.database.path), self.backend,
                                 policy=self.policy, clock=lambda: self.clock[0])

    def restart_store(self):
        self.store = StandaloneChatStore(SQLiteDatabase(self.database.path), vector_index=self.index,
                                        lexical_backend=self.lexical_backend)
        self.store.initialize()
        self.service = StandaloneChatService(self.store)

    def cache(self, memory, vector=(1., 0., 0.)):
        with self.database.connect() as conn:
            source = conn.execute("SELECT correlation_id FROM standalone_chat_messages WHERE message_id=?",
                                  (str(memory.source_message_id),)).fetchone()
        anchor = VectorAnchor(memory.memory_id, memory.room_id, memory.source_message_id, UUID(source[0]),
                              hashlib.sha256(memory.content.encode()).hexdigest())
        record = VectorRecord(anchor, self.backend.model, vector)
        SQLiteMemoryVectorBackend(self.database).upsert((record,))
        self.records[memory.memory_id] = record

    def save(self, text, *, room=None, vector=(1., 0., 0.), replaces=None, open_loop=False):
        room = room or self.room
        # Fixture cache generation is manual and deterministic, not a model call.
        self.store.memory.vector_index = None
        try:
            source = self.service.post_message(room, content="@白金 " + text,
                idempotency_key=uuid4(), reply_to=None).message
            if open_loop:
                memory = self.store.memory.capture_agent_proposal(room, source.message_id,
                    AgentMemoryProposal(kind="open_loop", content=text))
                require(memory is not None, "synthetic_open_loop_not_created")
            else:
                memory = self.service.create_memory(room, source_message_id=source.message_id,
                                                    content=text, replaces_memory_id=replaces)
            if vector is not None:
                self.cache(memory, vector)
            return memory
        finally:
            self.store.memory.vector_index = self.index

    def desired(self, memory):
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            current = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                                   (str(memory.memory_id),)).fetchone()
            return self.worker.desired_in_snapshot(conn, current)[0]

    def audit(self):
        maintenance = MemoryVectorMaintenance(self.worker)
        status = finish(maintenance, maintenance.start(mode="audit"), limit=2)
        require(status["consistent_at_observation"], "fresh_audit_inconsistent")
        require(not any(self.worker.status().get(s, 0) for s in ("pending", "processing", "waiting", "dead")),
                "outbox_not_drained")
        return status

    def run(self):
        gateway = self.save("gateway 读超时固定 8 秒", vector=(0., 1., 0.))
        semantic = self.save("异步任务使用队列")
        retired = self.save("撤销边界样本", vector=(0., 0., 1.))
        current = self.save("当前讨论边界样本")
        self.save("其他房间边界样本", room=self.other)
        self.worker.run_once(limit=200)
        originals = tuple(self.desired(m) for m in (semantic, retired))
        excluded = self.records[current.memory_id].anchor.source_correlation_id
        recall = lambda query: self.store.memory.retrieve(self.room, query, exclude_correlation_id=excluded)
        require(recall("后台如何解耦") == (semantic,), "initial_scoped_semantic_recall")

        self.control.stop()
        before = self.embedder.calls
        require(recall("gateway") == (gateway,) and self.embedder.calls == before, "outage_cheap_lexical_fallback")
        self.cache(semantic)  # New revision while storage is unavailable.
        self.store.memory.retire(self.room, retired.memory_id)
        fresh = self.save("新建恢复边界样本", vector=(0., 0., 1.))
        self.worker.run_once(limit=200)
        require(self.worker.run_once(limit=200) == 0, "retry_backoff_not_bounded")
        self.clock[0] += 2
        self.worker.run_once(limit=200)
        require(self.worker.status().get("dead", 0) >= 3, "outage_dead_letter_missing")
        require(self.worker.run_once(limit=200) == 0, "dead_letter_retried_automatically")
        outage_status = self.worker.status()
        self.restart_store()
        self.worker = self.new_worker()
        require(self.worker.status() == outage_status and recall("gateway") == (gateway,), "outage_restart_not_durable")
        self.checks.append("actual_or_offline_outage_cheap_fallback_bounded_dead_letters_and_sqlite_restart")

        self.control.start()
        require(all(i.code == "current" for i in self.backend.inspect_mutations(originals)), "remote_data_not_persistent")
        require(not recall("后台如何解耦"), "pending_revision_or_retired_point_leaked")
        require(self.worker.run_once(limit=200) == 0, "recovery_silently_redrove_dead_letters")
        require(self.worker.retry_dead(limit=200) >= 3, "explicit_redrive_failed")
        self.worker.run_once(limit=200)
        require(recall("后台如何解耦") == (semantic,), "recovery_recall_missing")
        for old in originals:
            require(self.backend.apply_mutation(old) > old.revision, "delayed_pre_outage_mutation_not_fenced")
        self.audit()
        self.checks.append("persistent_remote_restart_explicit_redrive_coalescing_old_write_fence_and_clean_audit")

        # Remote success, local ACK deliberately omitted. New worker instances
        # model the process crash window; this is NOT a real OS worker kill.
        self.cache(fresh, vector=(0., 0., 1.))
        claim = self.worker.claim()
        require(claim is not None, "ack_window_claim_missing")
        mutation, _ = self.worker._materialize(claim)
        self.backend.apply_mutation(mutation)
        self.worker = self.new_worker()
        require(self.worker.run_once() == 0, "active_lease_taken_over")
        self.clock[0] += 61
        require(self.worker.run_once() == 1 and not self.worker._finish(claim, "delivered"), "stale_ack_token_not_fenced")
        missing = self.save("缺缓存恢复边界样本", vector=None)
        require(self.worker.run_once() == 1 and self.worker.status().get("waiting") == 1, "missing_cache_not_waiting")
        self.cache(missing, vector=(0., 0., 1.))
        self.worker.run_once()
        self.audit()
        self.checks.append("process_equivalent_ack_loss_lease_takeover_and_missing_cache_new_revision_resume")

        for action in ("retire", "replace", "complete", "cancel", "physical_delete"):
            text = "生命周期" + action
            memory = self.save(text, open_loop=action in ("complete", "cancel"))
            self.worker.run_once()
            old = self.desired(memory)
            require(old.record is not None, "lifecycle_active_vector_missing")
            self.control.stop()
            if action == "retire":
                self.store.memory.retire(self.room, memory.memory_id)
            elif action == "replace":
                self.save("替代事实边界样本", replaces=memory.memory_id, vector=(0., 0., 1.))
            elif action in ("complete", "cancel"):
                self.service.close_open_loop(self.room, memory.memory_id, action=action)
            else:
                # Synthetic-only physical deletion, not a production deletion API.
                with self.database.transaction() as conn:
                    for table in ("standalone_chat_memory_embeddings", "standalone_chat_memory_fts_documents",
                                  "standalone_chat_memory_candidates", "standalone_chat_memories"):
                        conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (str(memory.memory_id),))
            require(memory.memory_id not in {m.memory_id for m in recall(text)}, "closed_memory_leaked_during_outage")
            self.control.start()
            self.restart_store()
            require(memory.memory_id not in {m.memory_id for m in recall(text)}, "closed_memory_resurrected_before_delivery")
            self.worker = self.new_worker()
            self.worker.run_once(limit=200)
            require(self.backend.apply_mutation(old) > old.revision, "lifecycle_old_write_not_fenced")
            require(self.backend.inspect_mutations((self.desired(memory),))[0].code == "current",
                    "lifecycle_tombstone_missing")
            require(memory.memory_id not in {m.memory_id for m in recall(text)}, "closed_memory_resurrected_after_delivery")
            self.checks.append("outage_restart_delayed_write_lifecycle_" + action)
        audit = self.audit()
        self.checks.append("final_two_way_audit_and_no_pending_processing_waiting_dead")
        return {"checks": self.checks, "outage_status": outage_status, "outbox_status": self.worker.status(),
                "final_audit": audit, "synthetic_query_vector_requests": self.embedder.calls,
                "worker_crash_scope": "ACK deliberately omitted and new worker instances; no OS kill"}


def probe(*, output_root=RESULTS):
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = OwnedDocker(output / "qdrant-storage")
    report = {"complete": False, "cleanup_complete": False, "embedding_calls": 0, "agent_calls": 0,
              "image": IMAGE, "container_name": control.name, "checks": [],
              "data": "isolated synthetic SQLite and vectors; owned loopback Docker; actual stop/start six times",
              "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
                  "evals/check_memory_qdrant_faults.py", "app/chat/vector_outbox.py",
                  "app/chat/qdrant_memory_index.py", "app/chat/qdrant_vector_sync.py",
                  "app/chat/vector_maintenance.py", "app/chat/memory.py")}}
    journey = None
    try:
        control.create()
        report["image_id"] = control.image_id
        control.start()
        endpoint = QdrantEndpoint(base_url=control.url, collection_prefix="codecrew_fault_probe", timeout_seconds=1)
        backend = QdrantVersionedVectorBackend(endpoint, VectorModel(SyntheticEmbedder.model_fingerprint, 3))
        backend.initialize()
        journey = FaultJourney(output / "synthetic.sqlite3", backend, endpoint, control)
        report.update(journey.run())
        report["fault_checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - never serialize raw Docker/provider bodies
        report["error_type"] = type(exc).__name__
    finally:
        if journey is not None:
            report["checks"] = journey.checks
            report["synthetic_query_vector_requests"] = journey.embedder.calls
        try:
            report["cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - refuse cleanup on ownership mismatch
            report["cleanup_error_type"] = type(exc).__name__
        report["complete"] = bool(report.get("fault_checks_passed") and report["cleanup_complete"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: --live creates a uniquely labelled loopback Docker container from local Qdrant 1.19.0; "
              "stops/starts only that container, checks recovery/lifecycle/audit, then removes only that container. "
              "Retains isolated evidence storage. No image pull, real chat DB, shared service, settings or models.")
        return
    output, report = probe()
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Fault probe incomplete; inspect safe report\n")
    print("Owned Qdrant actual outage/recovery/lifecycle/audit and container cleanup passed; embedding/Agent 0/0")


if __name__ == "__main__":
    main()
