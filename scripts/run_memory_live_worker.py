"""Operate an existing private acceptance target, never prepare or run models.

Default plan opens nothing. --status is a DB observation, not a heartbeat.
--watch is foreground; --check owns two subprocesses for start/diagnose/stop.
Manifest paths/model/namespace are validated; .env is never written or copied.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from app.chat.vector_worker import VectorWorkerStop, vector_worker_signals, watch_vector_worker
from app.chat.vector_worker_contracts import (
    check_vector_worker_target,
    requested_vector_worker_policy,
)
from app.chat.vector_worker_diagnostics import read_vector_worker_diagnostics
from app.config import Settings
from scripts.backup_memory_live_database import inventory, private_json
from scripts.prepare_memory_live_target import ROOT, TARGETS

PROVENANCE = (
    "scripts/run_memory_live_worker.py", "app/chat/vector_worker.py", "app/chat/vector_worker_contracts.py",
    "app/chat/vector_worker_diagnostics.py", "app/chat/vector_outbox.py", "app/chat/qdrant_vector_sync.py",
    "app/chat/qdrant_vector_backend.py", "app/config.py",
)
OVERRIDES = {"database_url", "memory_lexical_backend", "memory_vector_store", "memory_qdrant_url",
             "memory_qdrant_collection_prefix", "memory_embedding_mode", "memory_vector_worker_enabled",
             "standalone_chat_workspace_root", "standalone_chat_runtime_root"}


@dataclass(frozen=True)
class LiveTarget:
    manifest: Path
    directory: Path
    database: Path
    backend: object
    settings: object
    manifest_sha256: str


def require(condition, category):
    if not condition:
        raise ValueError(category)


def load_target(manifest, *, target_root=TARGETS, settings_factory=Settings,
                backend_factory=QdrantVersionedVectorBackend, watch=False):
    path = Path(manifest).resolve(strict=True)
    root = Path(target_root).resolve()
    require(path.name == "prepare.json" and path.parent.parent == root
            and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", path.parent.name), "private_target_manifest_required")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, "bounded_manifest_required")
    data = json.loads(raw)
    require(isinstance(data, dict) and type(data.get("version")) is int and data["version"] == 1
            and data.get("complete") is True and data.get("phase") == "ready_not_started", "prepared_target_required")
    directory = path.parent
    database = directory / "chat.sqlite3"
    require(data.get("directory") == str(directory) and data.get("database") == str(database)
            and database.resolve(strict=True) == database and database.is_file(), "manifest_database_scope_mismatch")
    values = data.get("runtime_overrides")
    require(isinstance(values, dict) and set(values) == OVERRIDES, "manifest_override_allowlist_required")
    require(values["database_url"] == "sqlite:///" + str(database)
            and values["standalone_chat_workspace_root"] == str(directory / "workspaces")
            and values["standalone_chat_runtime_root"] == str(directory / "runtime")
            and values["memory_lexical_backend"] == "bm25" and values["memory_vector_store"] == "qdrant"
            and values["memory_embedding_mode"] == "on" and values["memory_vector_worker_enabled"] is False,
            "manifest_runtime_scope_mismatch")
    prefix = values["memory_qdrant_collection_prefix"]
    require(isinstance(prefix, str) and re.fullmatch(r"codecrew_live_real_[a-f0-9]{16}", prefix), "dedicated_namespace_required")
    require(isinstance(data.get("model_fingerprint"), str)
            and re.fullmatch(r"[a-f0-9]{64}", data["model_fingerprint"]), "explicit_model_identity_required")
    model = VectorModel(data["model_fingerprint"], data.get("dimensions"))
    # Explicit arguments override caller environment/.env target settings; only
    # existing Qdrant credential is consumed. No embedding client is constructed.
    selected = {**values, "memory_embedding_mode": "off", "memory_vector_worker_enabled": watch,
                "memory_vector_worker_batch_size": 32, "memory_vector_worker_poll_interval_seconds": 2.,
                "memory_vector_worker_shutdown_grace_seconds": 10.}
    settings = settings_factory(_env_file=ROOT / ".env", **selected)
    endpoint = QdrantEndpoint(base_url=values["memory_qdrant_url"], collection_prefix=prefix,
        api_key=settings.memory_qdrant_api_key.get_secret_value(), timeout_seconds=settings.memory_qdrant_timeout_seconds)
    require(urlsplit(endpoint.base_url).hostname in ("localhost", "127.0.0.1", "::1"), "local_qdrant_required")
    backend = backend_factory(endpoint, model)
    require(backend.collection_name == data.get("collection") and backend.sync_target_id == data.get("target_id"),
            "manifest_backend_identity_mismatch")
    return LiveTarget(path, directory, database, backend, settings, hashlib.sha256(raw).hexdigest())


def diagnostics(target):
    return read_vector_worker_diagnostics(target.database, target.backend.sync_target_id)


def watch_target(target):
    policy = requested_vector_worker_policy(target.settings, requested=True)
    stop = VectorWorkerStop()
    with vector_worker_signals(stop):
        # This is a preflight receipt, not proof of the delivery loop being ready.
        # Runtime repeats its own preflight; final observed diagnostics prove it.
        check_vector_worker_target(target.database, target.backend)
        print(json.dumps({"event": "preflight_passed", "pid": os.getpid(), "target_id": target.backend.sync_target_id}), flush=True)
        result = watch_vector_worker(target.database, target.backend, policy, stop=stop)
    payload = {"event": "watch_exited", "reason": result.reason, "attempted": result.attempted,
               "clean_shutdown": result.exit_code == 0, "diagnostics": result.diagnostics,
               "diagnostics_status": result.diagnostics_status, "embedding_calls": 0, "agent_calls": 0}
    print(json.dumps(payload), flush=True)
    return result.exit_code


def child_environment():
    # No inherited keys, HOME/CODEX_HOME overrides or parent CODECREW_* target.
    return {"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}


def wait_until(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise TimeoutError("worker_checkpoint_timeout")


def cleanup_child(proc):
    # Only a Popen created in this invocation. No PID files/name/group kills.
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    proc.communicate(timeout=5)
    return proc.poll() is not None


def check_target(target, *, launcher=subprocess.Popen, hold_seconds=4.2):
    require(type(hold_seconds) in (int, float) and 4 <= hold_seconds <= 10, "two_default_polls_required")
    check_vector_worker_target(target.database, target.backend)  # Read-only, no allocation on refusal.
    before = inventory(target.database)
    require(before["counts"]["standalone_chat_vector_outbox"] == 0
            and before["counts"]["standalone_chat_memories"] == 0, "empty_target_check_required")
    parent = target.directory / "worker-checks"
    parent.mkdir(exist_ok=True)
    output = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(mode=0o700, exist_ok=False)
    report = {"complete": False, "manifest": str(target.manifest), "manifest_sha256": target.manifest_sha256,
              "database": str(target.database), "target_id": target.backend.sync_target_id,
              "inventory_before": before, "processes": [], "worker_running_after_check": False,
              "embedding_calls": 0, "agent_calls": 0, "private_env_changed": False, "chat_started": False,
              "policy": {"batch_size": 32, "poll_seconds": 2., "grace_seconds": 10.},
              "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROVENANCE}}
    children, handles = [], []
    try:
        for signum, label in ((signal.SIGTERM, "sigterm"), (signal.SIGINT, "sigint")):
            directory = output / label
            directory.mkdir(mode=0o700)
            stdout = directory / "stdout.jsonl"
            stderr = directory / "stderr.log"
            for path in (stdout, stderr):
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            out, err = stdout.open("w"), stderr.open("w")
            handles.extend((out, err))
            proc = launcher([sys.executable, "-m", "scripts.run_memory_live_worker", "--watch", "--manifest", str(target.manifest)],
                            cwd=directory, env=child_environment(), stdin=subprocess.DEVNULL,
                            stdout=out, stderr=err, start_new_session=True)
            children.append(proc)
            require(type(proc.pid) is int and proc.pid > 0 and proc.pid != os.getpid(), "owned_child_pid_required")
            def ready(proc=proc, stdout=stdout):
                require(proc.poll() is None, "worker_exited_before_checkpoint")
                lines = stdout.read_text().splitlines()
                if not lines:
                    return False
                try:
                    event = json.loads(lines[0])
                except json.JSONDecodeError:
                    return False  # Writer may be between write/flush.
                require(event == {"event": "preflight_passed", "pid": proc.pid, "target_id": target.backend.sync_target_id},
                        "child_preflight_identity_mismatch")
                return True
            wait_until(ready)
            started = time.monotonic()
            def observed(proc=proc, started=started):
                require(proc.poll() is None, "worker_exited_during_observation")
                return time.monotonic() - started >= hold_seconds
            wait_until(observed, timeout=12)
            frame = diagnostics(target)
            require(frame["backlog_total"] == 0 and frame["target_registered"], "unexpected_live_backlog")
            proc.send_signal(signum)
            proc.wait(timeout=15)  # Actual default 10s grace plus bounded supervisor margin.
            proc.communicate(timeout=5)
            out.close()
            err.close()
            raw = stdout.read_text()
            require(len(raw) <= 16384 and stderr.stat().st_size == 0, "unexpected_child_output")
            events = [json.loads(line) for line in raw.splitlines()]
            require(len(events) == 2, "unexpected_child_event_count")
            final = events[-1]
            require(proc.returncode == 0 and final["event"] == "watch_exited" and final["reason"] == "stopped"
                    and final["clean_shutdown"] and final["attempted"] == 0
                    and final["embedding_calls"] == final["agent_calls"] == 0
                    and final["diagnostics_status"] == "observed" and final["diagnostics"]["backlog_total"] == 0,
                    "actual_watch_clean_stop_failed")
            report["processes"].append({"pid": proc.pid, "signal": label, "returncode": proc.returncode,
                                         "pre_stop_database_diagnostics": frame, "final": final})
        require(len({p["pid"] for p in report["processes"]}) == 2, "distinct_processes_required")
        report["inventory_after"] = inventory(target.database)
        require(report["inventory_after"] == before, "empty_target_changed_by_check")
        require(hashlib.sha256(target.manifest.read_bytes()).hexdigest() == target.manifest_sha256,
                "manifest_changed_during_check")
        require(all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha for p, sha in report["code_sha256"].items()),
                "worker_code_changed_during_check")
        report["checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - never emit env/provider/SQL text
        report["error_type"] = type(exc).__name__
    finally:
        cleaned = []
        for proc in children:
            try:
                cleaned.append(cleanup_child(proc))
            except Exception as exc:  # noqa: BLE001 - still attempt every owned child
                cleaned.append(False)
                report["cleanup_error_type"] = type(exc).__name__
        for handle in handles:
            handle.close()
        report["children_cleanup_complete"] = all(cleaned)
        report["worker_running_after_check"] = any(proc.poll() is None for proc in children)
        report["complete"] = bool(report.get("checks_passed") and report["children_cleanup_complete"])
        private_json(output / "report.json", report)
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--watch", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--check", action="store_true", help="bounded start/diagnose/SIGTERM+SIGINT stop acceptance")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    if not (args.watch or args.status or args.check):
        print("Plan only: --manifest existing private prepared target; --status DB diagnostics (not heartbeat), "
              "--watch foreground worker only, --check two owned OS children then stop/reap. "
              "No manifest/Settings/.env opened, migrations/preparation/embedding/Agents/chat/service installation.")
        return
    if args.manifest is None:
        parser.error("explicit --manifest required")
    try:
        target = load_target(args.manifest, watch=args.watch)
        if args.watch:
            code = watch_target(target)
            if code:
                parser.exit(code, "Worker exited without clean shutdown\n")
        elif args.status:
            print(json.dumps({"observation": "database_only_not_process_heartbeat", "diagnostics": diagnostics(target),
                              "embedding_calls": 0, "agent_calls": 0}))
        else:
            output, report = check_target(target)
            print(f"Worker acceptance report: {output / 'report.json'}")
            if not report["complete"]:
                parser.exit(1, "Worker acceptance incomplete; inspect retained evidence\n")
            print("Actual worker start/diagnose/SIGTERM+SIGINT stop passed; children reaped; worker stopped; models 0/0")
    except Exception as exc:  # noqa: BLE001 - no private Settings or provider error text
        parser.exit(1, f"Worker operation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
