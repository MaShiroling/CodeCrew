"""Plan-only by default: actual OS signals/restarts + one owned Qdrant Docker.

Uses real 60-second leases and default five attempts; no time/SQL shortcuts.
Clean child env/cwd, synthetic vectors, no .env/keys/Agents/real chat/shared 6333.
Keep all SQLite/markers/report/bind evidence; remove only owned container/children.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from evals.benchmark_memory_qdrant import finish_audit
from evals.check_memory_qdrant_faults import (
    IMAGE,
    ROOT,
    FaultJourney,
    OwnedDocker,
    SyntheticEmbedder,
    require,
)

RESULTS = ROOT / "evals/results/memory-vector-worker-process-v1"
PROVENANCE = ("evals/check_memory_vector_worker_processes.py", "evals/memory_vector_worker_child.py",
              "evals/check_memory_qdrant_faults.py", "evals/benchmark_memory_qdrant.py", "app/config.py",
              "app/chat/vector_sync.py", "app/chat/vector_worker.py", "app/chat/vector_worker_contracts.py",
              "app/chat/vector_worker_diagnostics.py", "app/chat/vector_worker_telemetry_schema.py",
              "app/chat/vector_outbox.py", "app/chat/vector_outbox_schema.py", "app/chat/qdrant_vector_sync.py",
              "app/chat/qdrant_vector_backend.py", "app/chat/vector_maintenance.py", "app/chat/memory.py",
              "app/chat/service.py", "app/chat/store.py", "app/chat/sqlite_vector_backend.py", "app/storage/sqlite.py")


def wait_until(predicate, *, timeout=15):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise RuntimeError("process_checkpoint_timeout")


def child_environment(endpoint, *, grace):
    # Never copy os.environ: no cloud keys, parent CODECREW_ config or HOME override.
    return {"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1",
            "CODECREW_MEMORY_VECTOR_WORKER_ENABLED": "true",
            "CODECREW_MEMORY_VECTOR_WORKER_BATCH_SIZE": "32",
            "CODECREW_MEMORY_VECTOR_WORKER_POLL_INTERVAL_SECONDS": ".1",
            "CODECREW_MEMORY_VECTOR_WORKER_SHUTDOWN_GRACE_SECONDS": str(grace),
            "CODECREW_MEMORY_QDRANT_URL": endpoint.base_url,
            "CODECREW_MEMORY_QDRANT_COLLECTION_PREFIX": endpoint.collection_prefix,
            "CODECREW_MEMORY_QDRANT_TIMEOUT_SECONDS": "5"}


class OwnedChild:
    def __init__(self, directory, journey, endpoint, *, mode="none", grace=2, launcher=subprocess.Popen):
        from evals.memory_vector_worker_child import MODES
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", endpoint.base_url)
        require(mode in MODES and match is not None and 1 <= int(match[1]) <= 65535
                and int(match[1]) != 6333 and not endpoint.api_key
                and endpoint.collection_prefix.startswith("codecrew_worker_"), "isolated_child_endpoint_required")
        self.directory = Path(directory).resolve()
        self.directory.mkdir(exist_ok=False)
        self.token, self.proc, self.result = uuid4().hex, None, None
        spec = {"directory": str(self.directory), "token": self.token, "mode": mode,
                "database": str(journey.database.path.resolve()), "model": journey.backend.model.fingerprint,
                "dimensions": journey.backend.model.dimensions}
        require(journey.database.path.resolve().parent == self.directory.parent, "child_database_scope_mismatch")
        self.spec = self.directory / "spec.json"
        self.spec.write_text(json.dumps(spec), encoding="utf-8")
        self.environment = child_environment(endpoint, grace=grace)
        self.launcher, self.mode = launcher, mode

    def start(self):
        require(self.proc is None and not (self.directory/".env").exists(), "child_already_started_or_private_env")
        self.proc = self.launcher([sys.executable, "-m", "evals.memory_vector_worker_child", "--spec", str(self.spec)],
            cwd=self.directory, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        require(self.proc.pid > 0 and self.proc.pid != os.getpid(), "child_identity_invalid")
        return self

    def marker(self, phase):
        path = self.directory / (self.token+"-"+phase+".json")
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        require(data == {"token": self.token, "phase": phase}, "child_marker_identity_mismatch")
        return True

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def send(self, signum):
        require(self.alive() and signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL), "owned_live_child_required")
        self.proc.send_signal(signum)  # Only our tracked Popen handle; no process-group or name kill.

    def release(self):
        require(self.mode != "none" and self.marker("gate"), "owned_gate_required")
        (self.directory/(self.token+"-release.json")).write_text(json.dumps({"token": self.token}), encoding="utf-8")

    def reap(self, *, timeout=5):
        require(self.proc is not None, "child_not_started")
        if self.result is None:
            out, err = self.proc.communicate(timeout=timeout)
            require(len(out) <= 16384 and len(err) <= 16384, "child_output_unbounded")
            code = self.proc.returncode
            report = json.loads(out) if out.strip() else None
            if report is not None:
                require(set(report) == {"reason", "attempted", "clean_shutdown", "diagnostics", "diagnostics_status",
                                        "embedding_calls", "agent_calls"}, "unexpected_child_output")
                require(report["embedding_calls"] == report["agent_calls"] == 0, "child_provider_calls")
            self.result = {"pid": self.proc.pid, "returncode": code, "report": report,
                           "stderr_present": bool(err), "mode": self.mode}
        return self.result

    def cleanup(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc is not None and self.result is None:
            self.reap()
        return not self.alive()


class ProcessJourney:
    def __init__(self, output, control, *, progress=print):
        self.output, self.control, self.progress = output, control, progress
        self.children, self.journeys, self.checks = [], [], []
        self.recoveries = []

    def fixture(self, name):
        directory = self.output/name
        directory.mkdir(exist_ok=False)
        endpoint = QdrantEndpoint(base_url=self.control.url, collection_prefix="codecrew_worker_"+name, timeout_seconds=5)
        backend = QdrantVersionedVectorBackend(endpoint, VectorModel(SyntheticEmbedder.model_fingerprint, 3))
        backend.initialize()  # Explicit preparation of this owned test target only.
        journey = FaultJourney(directory/"chat.sqlite3", backend, endpoint, self.control)
        self.journeys.append((name, journey))
        return journey, endpoint

    def spawn(self, journey, endpoint, name, **options):
        child = OwnedChild(journey.database.path.parent/name, journey, endpoint, **options)
        self.children.append(child)  # Record before start to clean partial launches safely.
        child.start()
        return child

    def row(self, journey, memory):
        with journey.database.connect() as conn:
            row = conn.execute("SELECT status,attempts,lease_token,lease_until FROM standalone_chat_vector_outbox "
                "WHERE target_id=? AND memory_id=? ORDER BY revision DESC LIMIT 1",
                (journey.worker.target_id, str(memory.memory_id))).fetchone()
        return dict(row) if row else None

    def wait_delivered(self, journey, memory, child, *, timeout=15):
        def complete():
            require(child.alive(), "worker_exited_before_delivery")
            return self.row(journey, memory)["status"] == "delivered"
        wait_until(complete, timeout=timeout)

    def finish_child(self, child, signum=signal.SIGTERM):
        child.send(signum)
        result = child.reap()
        require(result["returncode"] == 0 and result["report"]["reason"] == "stopped"
                and result["report"]["clean_shutdown"], "graceful_exit_failed")
        return result

    def record(self, name, **data):
        self.checks.append({"check": name, **data})
        self.progress("[process] "+name, flush=True)

    def graceful(self):
        journey, endpoint = self.fixture("graceful_idle")
        memory = journey.save("进程空闲停止样本")
        child = self.spawn(journey, endpoint, "worker")
        self.wait_delivered(journey, memory, child)
        result = self.finish_child(child, signal.SIGINT)
        require(result["report"]["diagnostics"]["last_delivered_at_unix"] is not None, "persisted_success_missing")
        self.record("actual_idle_SIGINT", process=result)

        journey, endpoint = self.fixture("graceful_inflight")
        memory = journey.save("在途停止样本")
        child = self.spawn(journey, endpoint, "worker", mode="after_remote")
        wait_until(lambda: child.marker("gate"))
        require(self.row(journey, memory)["status"] == "processing", "inflight_claim_missing")
        sentinel = journey.save("停止后不应继续领取")
        child.send(signal.SIGTERM)
        wait_until(lambda: child.marker("stop"))
        child.release()
        result = child.reap()
        require(result["returncode"] == 0 and self.row(journey, memory)["status"] == "delivered"
                and self.row(journey, sentinel)["attempts"] == 0, "inflight_stop_claimed_next")
        restart = self.spawn(journey, endpoint, "restart")
        self.wait_delivered(journey, sentinel, restart)
        restarted = self.finish_child(restart)
        self.record("actual_inflight_SIGTERM_finishes_only_claim", process=result, restart=restarted)

    def abrupt(self, name, mode, *, timeout_stop=False):
        journey, endpoint = self.fixture(name)
        memory = journey.save("进程租约恢复样本")
        child = self.spawn(journey, endpoint, "worker", mode=mode, grace=.2 if timeout_stop else 2)
        wait_until(lambda: child.marker("gate"))
        before = self.row(journey, memory)
        require(before["status"] == "processing" and before["attempts"] == 1
                and before["lease_until"] > time.time(), "active_real_lease_missing")
        remote = journey.backend.inspect_mutations((journey.desired(memory),))[0]
        require(remote.code == ("missing" if mode == "before_remote" else "current"), "remote_commit_boundary_not_proven")
        child.send(signal.SIGTERM if timeout_stop else signal.SIGKILL)
        killed = child.reap()
        expected = 1 if timeout_stop else -signal.SIGKILL
        require(killed["returncode"] == expected, "actual_exit_code_mismatch")
        if timeout_stop:
            require(killed["report"]["reason"] == "shutdown_timeout"
                    and not killed["report"]["clean_shutdown"], "timeout_report_missing")
        after = self.row(journey, memory)
        require(after == before and journey.worker.diagnostics()["last_delivered_at_unix"] is None,
                "uncertain_lease_or_success_changed")
        restarted = self.spawn(journey, endpoint, "restart")
        wait_until(lambda: restarted.marker("ready"))
        time.sleep(.15)  # Real pre-expiry observation, not lease mutation or synthetic clock.
        require(time.time() < before["lease_until"] and self.row(journey, memory) == before,
                "restart_claimed_unexpired_lease")
        require(restarted.proc.pid != child.proc.pid, "restart_pid_not_distinct")
        self.recoveries.append((name, journey, memory, restarted, before, killed, remote.code))
        self.progress("[process] "+name+" killed; restarted before lease expiry", flush=True)

    def outage(self):
        journey, endpoint = self.fixture("outage")
        refused, refused_endpoint = self.fixture("startup_refusal")
        child = self.spawn(journey, endpoint, "worker")
        wait_until(lambda: child.marker("ready"))
        self.control.stop()
        try:
            refusal = self.spawn(refused, refused_endpoint, "worker")
            refused_result = refusal.reap()
            require(refused_result["returncode"] == 1 and refused_result["report"]["reason"] == "startup_failed"
                    and not refused_result["report"]["attempted"] and refused.worker.status() == {}, "startup_outage_not_refused")
            self.record("actual_startup_outage_refusal", process=refused_result)
            memory = journey.save("真实停服有限重试样本")
            wait_until(lambda: self.row(journey, memory)["status"] == "dead", timeout=50)
            require(child.alive() and self.row(journey, memory)["attempts"] == 5, "default_attempt_bound_not_observed")
            self.progress("[process] outage reached dead after 5 actual attempts", flush=True)
        finally:
            self.control.start()
        time.sleep(.3)
        require(self.row(journey, memory)["status"] == "dead" and self.row(journey, memory)["attempts"] == 5,
                "dead_automatically_redriven")
        require(journey.worker.retry_dead(limit=1) == 1, "explicit_fixture_redrive_failed")
        self.wait_delivered(journey, memory, child)
        result = self.finish_child(child)
        self.record("actual_outage_default_backoff_dead_explicit_redrive", process=result,
                    attempts_before_redrive=5, attempts_after_redrive=self.row(journey, memory)["attempts"])

    def run(self):
        self.graceful()
        self.abrupt("timeout_after_ack", "after_remote", timeout_stop=True)
        self.abrupt("kill_before_remote", "before_remote")
        self.abrupt("kill_after_ack", "after_remote")
        self.outage()
        for name, journey, memory, child, before, killed, remote in self.recoveries:
            remaining = max(0., before["lease_until"]-time.time())
            self.progress(f"[process] {name}: waiting for real lease expiry ({remaining:.1f}s)", flush=True)
            self.wait_delivered(journey, memory, child, timeout=remaining+20)
            after = self.row(journey, memory)
            require(after["attempts"] == 2 and after["lease_token"] is None
                    and journey.backend.inspect_mutations((journey.desired(memory),))[0].code == "current", "lease_recovery_inconsistent")
            result = self.finish_child(child)
            self.record("actual_"+name+"_real_lease_recovery", old=killed, restart=result,
                        old_remote_state=remote, lease_seconds=60, attempts=2, no_preexpiry_takeover=True)
        audits = {}
        for name, journey in self.journeys:
            audit = finish_audit(journey.worker)
            audits[name] = {"counts": audit["counts"], "consistent": audit["consistent_at_observation"],
                            "outbox": journey.worker.status(), "diagnostics": journey.worker.diagnostics()}
            require(not any(journey.worker.status().get(s) for s in ("pending", "processing", "waiting", "dead")), "final_backlog_not_empty")
        return {"checks": self.checks, "audits": audits, "children": len(self.children),
                "scope": "actual owned OS processes/signals; real 60s leases; deterministic eval-only before/after remote-ACK gates; not crash-at-arbitrary-instruction or SLA",
                "embedding_calls": 0, "agent_calls": 0}

    def cleanup(self):
        results = []
        for child in self.children:
            try:
                results.append(child.cleanup())
            except Exception:  # noqa: BLE001 - still attempt every other owned child
                results.append(False)
        return all(results)


def run_suite(*, output_root=RESULTS):
    require(sys.platform != "win32", "POSIX_signals_required")
    output = output_root/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = OwnedDocker(output/"qdrant-storage")
    journey = ProcessJourney(output, control)
    report = {"complete": False, "cleanup_complete": False, "image": IMAGE, "python": platform.python_version(),
              "embedding_calls": 0, "agent_calls": 0, "default_changed": False,
              "code_sha256": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PROVENANCE}}
    def checkpoint():
        (output/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint()
    try:
        control.create()
        control.start()
        report["image_id"], report["container_name"] = control.image_id, control.name
        report.update(journey.run())
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == sha for p, sha in report["code_sha256"].items()),
                "code_changed_during_process_matrix")
        report["matrix_passed"] = True
    except Exception as exc:  # noqa: BLE001 - no raw error/body/env/SQL/fact output
        report["error_type"] = type(exc).__name__
        report["completed_checks"] = journey.checks
    finally:
        try:
            report["children_cleanup_complete"] = journey.cleanup()
        except Exception as exc:  # noqa: BLE001 - never delete foreign resources to recover cleanup
            report["children_cleanup_error_type"] = type(exc).__name__
        try:
            report["container_cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - ownership gate remains mandatory
            report["container_cleanup_error_type"] = type(exc).__name__
        report["cleanup_complete"] = bool(report.get("children_cleanup_complete") and report.get("container_cleanup_complete"))
        report["complete"] = bool(report.get("matrix_passed") and report["cleanup_complete"])
        checkpoint()
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: real owned OS SIGINT/SIGTERM/SIGKILL/restarts, natural 60-second leases, actual "
              "outage/default five attempts/dead/explicit fixture redrive, fresh audits in one new owned Qdrant. "
              "Clean subprocess env/cwd; no settings/.env/keys/cloud/Agents/shared 6333/real chat/default change.")
        return
    output, report = run_suite()
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Process matrix incomplete; not scored as memory misses\n")
    print("Actual OS process matrix passed; children/container cleanup complete; models 0/0")


if __name__ == "__main__":
    main()
