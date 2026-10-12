"""Explicit standalone watch runtime; never called by chat startup.

One daemon delivery thread inside the dedicated CLI process. The main thread
supervises stop/grace; timeout is non-clean and must exit that process. Returning
from this helper alone does NOT kill a blocked thread; do not embed in a server.
"""

import queue
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import (
    VectorWorkerPolicy,
    VectorWorkerStopRequest,
    check_vector_worker_target,
)
from app.storage import SQLiteDatabase


class _ExistingWorkerDatabase(SQLiteDatabase):
    """Delivery may write an existing prepared DB, never silently create one."""
    def _open_connection(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                               timeout=self.busy_timeout_ms/1000, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            conn.close()
            raise
        return conn


class VectorWorkerStop:
    """Signal-safe CPython request recording; no Event/DB/HTTP in the handler.

    SimpleQueue.put is reentrant in CPython. Supervisor drains timestamps and
    wakes Event outside signal handlers. Repeated requests cannot renew grace.
    """
    def __init__(self, *, clock=time.monotonic):
        if sys.implementation.name != "cpython":
            raise RuntimeError("worker signal supervision requires CPython")
        self.clock = clock
        self.requested = False
        self.requests = queue.SimpleQueue()
        self.wake = threading.Event()
        self.window = None

    def request_stop(self):
        if not self.requested:
            self.requests.put(self.clock())
            self.requested = True

    def observe(self, grace):
        while True:
            try:
                timestamp = self.requests.get_nowait()
            except queue.Empty:
                break
            if self.window is None or timestamp < self.window.requested_at:
                self.window = VectorWorkerStopRequest(timestamp, grace)
        if self.window is not None:
            self.wake.set()
        return self.window


@contextmanager
def vector_worker_signals(stop):
    """Only explicit CLI watch installs handlers; restore even on failure."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("worker signal supervision requires main thread")
    originals = {}
    def handler(signum, frame):
        stop.request_stop()
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, handler)
            originals[signum] = previous
        yield
    finally:
        for signum, previous in originals.items():
            signal.signal(signum, previous)


@dataclass(frozen=True, slots=True)
class VectorWorkerResult:
    reason: str  # fixed categories only, never exception/provider messages
    attempted: int
    diagnostics: dict | None = None
    diagnostics_status: str = "not_observed"

    @property
    def exit_code(self):
        return 0 if self.reason == "stopped" else 1


def watch_vector_worker(database_path, backend, policy, *, stop=None):
    """Foreground CLI supervision, preflight included in the stop budget.

    This function requires an already-approved policy; no settings or embedding
    client. On timeout the caller MUST exit its standalone process; uncertain
    in-flight leases are untouched. A stop race can overlap one accepted claim.
    """
    if not isinstance(policy, VectorWorkerPolicy):
        raise TypeError("typed worker policy required")
    stop = stop or VectorWorkerStop()
    if not isinstance(stop, VectorWorkerStop):
        raise TypeError("typed worker stop controller required")
    done = threading.Event()
    state = {"reason": "startup_failed", "attempted": 0, "finished_at": None,
             "diagnostics": None, "diagnostics_status": "not_observed"}
    def observe(worker):
        try:
            state["diagnostics"] = worker.diagnostics()
            state["diagnostics_status"] = "observed"
        except Exception:  # noqa: BLE001 - telemetry read failure must not alter delivery/lease behavior
            state["diagnostics_status"] = "unavailable"
    def result(reason):
        return VectorWorkerResult(reason, state["attempted"], state["diagnostics"],
                                  "stale" if reason == "shutdown_timeout" and state["diagnostics"] else state["diagnostics_status"])
    def delivery():
        started = False
        worker = None
        try:
            if stop.requested:
                state["reason"] = "stopped"
                return
            target = check_vector_worker_target(database_path, backend)
            if stop.requested:
                state["reason"] = "stopped"
                return
            worker = MemoryVectorOutbox(_ExistingWorkerDatabase(target.database_path), backend)
            started = True
            state["reason"] = "worker_loop_failed"
            observe(worker)
            while not stop.requested:
                for _ in range(policy.batch_size):
                    if stop.requested:
                        break
                    count = worker.run_once(limit=1, should_stop=lambda: stop.requested)
                    if type(count) is not int or count not in (0, 1):
                        raise RuntimeError("invalid single-event delivery count")
                    state["attempted"] += count
                    if not count:
                        break
                if not stop.requested:
                    observe(worker)
                    stop.wake.wait(policy.poll_interval_seconds)
            state["reason"] = "stopped"
        except BaseException:  # noqa: BLE001 - no thread traceback/body, unexpected exits are failures
            state["reason"] = "worker_loop_failed" if started else "startup_failed"
        finally:
            if worker is not None:
                observe(worker)
            state["finished_at"] = stop.clock()
            done.set()
    thread = threading.Thread(target=delivery, name="codecrew-vector-delivery", daemon=True)
    thread.start()
    while True:
        window = stop.observe(policy.shutdown_grace_seconds)
        if done.is_set():
            thread.join(timeout=.05)
            if thread.is_alive() or (window is not None and window.remaining_seconds(
                    max(state["finished_at"], window.requested_at)) <= 0):
                return result("shutdown_timeout")
            return result(state["reason"])
        if window is not None and window.remaining_seconds(stop.clock()) <= 0:
            return result("shutdown_timeout")
        # Signal handlers run on this main thread. Keep wait bounded even when
        # the delivery thread is blocked in HTTP/SQLite; no unbounded join.
        remaining = window.remaining_seconds(stop.clock()) if window else .05
        done.wait(min(.05, remaining))
