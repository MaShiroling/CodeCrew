"""Watch runtime with real temporary SQLite and fake backend; no OS-kill claims."""

import signal
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest

from app.chat import vector_sync
from app.chat import vector_worker as runtime
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import VectorWorkerPolicy
from app.config import Settings
from app.storage import SQLiteDatabase
from tests.test_memory_vector_outbox import VersionedFake, cache, rows, save


@pytest.fixture(autouse=True)
def no_private_or_network(monkeypatch):
    import os
    import urllib.request
    for name in tuple(os.environ):
        if name.startswith("CODECREW_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(vector_sync, "Settings", lambda: Settings(_env_file=None))


@pytest.fixture
def prepared(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "watch.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="worker fixture", idempotency_key=uuid4()).room_id
    backend = VersionedFake()
    backend._require_sync_ready = lambda: None
    worker = MemoryVectorOutbox(store.database, backend)
    worker.register_target()
    return store, service, room, backend, worker


def watch(prepared, *, stop, policy=None):
    return runtime.watch_vector_worker(prepared[0].database.path, prepared[3],
                                       policy or VectorWorkerPolicy(32, .1, 1), stop=stop)


def seed(prepared, n=1, *, cached=True):
    store, service, room, _, _ = prepared
    memories = [save(service, room, f"合成事实 {i}") for i in range(n)]
    if cached:
        for memory in memories:
            cache(store, memory)
    return memories


def test_stop_before_start_never_preflights(monkeypatch):
    stop = runtime.VectorWorkerStop()
    stop.request_stop()
    monkeypatch.setattr(runtime, "check_vector_worker_target", lambda *a: pytest.fail("stopped startup probed"))
    result = runtime.watch_vector_worker(Path("absent"), None, VectorWorkerPolicy(), stop=stop)
    assert result.reason == "stopped" and result.exit_code == result.attempted == 0


def test_single_event_checkpoints_stop_inside_batch_preserve_next(prepared, monkeypatch):
    seed(prepared, 3)
    stop, limits = runtime.VectorWorkerStop(), []
    original = MemoryVectorOutbox.run_once
    def observed(self, *, limit, should_stop):
        limits.append(limit)
        return original(self, limit=limit, should_stop=should_stop)
    monkeypatch.setattr(MemoryVectorOutbox, "run_once", observed)
    prepared[3].after_apply = stop.request_stop
    result = watch(prepared, stop=stop)
    assert limits == [1] and result.attempted == 1 and result.reason == "stopped"
    assert len(prepared[3].calls) == 1
    assert prepared[4].status()["pending"] == 2 and prepared[4].status()["delivered"] == 1


def test_claim_checks_stop_after_write_lock_acquisition(prepared):
    seed(prepared)
    calls = []
    def stop_after_lock():
        calls.append(1)
        return len(calls) == 2
    before = rows(prepared[0])
    assert prepared[4].run_once(limit=1, should_stop=stop_after_lock) == 0
    assert rows(prepared[0]) == before and prepared[3].calls == []


def test_claim_checks_again_before_leasing(prepared):
    seed(prepared)
    calls = []
    def before_lease():
        calls.append(1)
        return len(calls) == 3
    assert prepared[4].claim(should_stop=before_lease) is None
    assert not any(row["attempts"] or row["status"] == "processing" for row in rows(prepared[0]))


def test_stop_after_accepted_claim_finishes_only_that_event(prepared, monkeypatch):
    seed(prepared, 2)
    stop = runtime.VectorWorkerStop()
    original = MemoryVectorOutbox.claim
    def claim(self, **kwargs):
        value = original(self, **kwargs)
        if value:
            stop.request_stop()
        return value
    monkeypatch.setattr(MemoryVectorOutbox, "claim", claim)
    result = watch(prepared, stop=stop)
    assert result.attempted == 1 and result.reason == "stopped" and len(prepared[3].calls) == 1
    assert prepared[4].status()["pending"] == 1


def test_idle_poll_sixty_seconds_interrupts_promptly(prepared, monkeypatch):
    stop, entered = runtime.VectorWorkerStop(), threading.Event()
    original = MemoryVectorOutbox.run_once
    def observed(self, **kwargs):
        result = original(self, **kwargs)
        entered.set()
        return result
    monkeypatch.setattr(MemoryVectorOutbox, "run_once", observed)
    requester = threading.Thread(target=lambda: (entered.wait(2), stop.request_stop()))
    requester.start()
    started = time.monotonic()
    result = watch(prepared, stop=stop, policy=VectorWorkerPolicy(32, 60, 1))
    requester.join(2)
    assert result.reason == "stopped" and result.attempted == 0
    assert time.monotonic()-started < 1 and not requester.is_alive()


def test_new_event_after_empty_poll_is_delivered(prepared, monkeypatch):
    stop, rounds = runtime.VectorWorkerStop(), []
    original = MemoryVectorOutbox.run_once
    def observed(self, **kwargs):
        value = original(self, **kwargs)
        rounds.append(value)
        if len(rounds) == 1:
            seed(prepared)
        return value
    monkeypatch.setattr(MemoryVectorOutbox, "run_once", observed)
    prepared[3].after_apply = stop.request_stop
    result = watch(prepared, stop=stop)
    assert rounds == [0, 1] and result.attempted == 1
    assert prepared[4].status() == {"superseded": 1, "delivered": 1}


def test_waiting_cache_not_computed_or_retried(prepared):
    seed(prepared, cached=False)
    stop = runtime.VectorWorkerStop()
    prepared[3].after_apply = stop.request_stop
    result = watch(prepared, stop=stop)
    assert result.reason == "stopped" and prepared[4].status() == {"waiting": 1}
    assert prepared[3].calls[0].record is None


def test_backoff_max_attempts_dead_no_automatic_redrive(prepared, monkeypatch):
    seed(prepared)
    store, _, _, backend, worker = prepared
    clock, sleeps = [100.], []
    worker.clock = lambda: clock[0]
    backend.fail = True
    stop = runtime.VectorWorkerStop()
    class Wait:
        def wait(self, seconds):
            sleeps.append(seconds)
            if worker.status().get("dead"):
                stop.request_stop()
            else:
                clock[0] = max(row["available_at"] for row in rows(store))
        def set(self):
            pass
    stop.wake = Wait()
    monkeypatch.setattr(runtime, "MemoryVectorOutbox", lambda *a: worker)
    result = watch(prepared, stop=stop)
    assert result.attempted == 5 and len(backend.calls) == 5 and len(sleeps) == 5
    assert worker.status() == {"dead": 1, "superseded": 1}
    active = next(r for r in rows(store) if r["status"] == "dead")
    assert active["attempts"] == 5 and active["error_code"] == "delivery_failed"


def test_timeout_unknown_ack_keeps_processing_until_delivery_actually_finishes(prepared):
    seed(prepared, 2)
    store, _, _, backend, worker = prepared
    stop, entered, release, ended = runtime.VectorWorkerStop(), threading.Event(), threading.Event(), threading.Event()
    def blocked():
        entered.set()
        release.wait(2)
        ended.set()
    backend.after_apply = blocked
    requester = threading.Thread(target=lambda: (entered.wait(2), stop.request_stop()))
    requester.start()
    try:
        result = watch(prepared, stop=stop, policy=VectorWorkerPolicy(32, .1, .1))
        assert result.reason == "shutdown_timeout" and result.exit_code == 1
        assert result.diagnostics_status == "stale" and result.diagnostics is not None
        assert worker.status()["processing"] == worker.status()["pending"] == 1
        processing = next(r for r in rows(store) if r["status"] == "processing")
        assert processing["lease_token"] and processing["lease_until"] > time.time()
        assert len(backend.calls) == 1  # fake remote committed, local ACK still unknown
    finally:
        release.set()
        requester.join(2)
        assert ended.wait(2)
        for _ in range(100):
            if worker.status().get("delivered"):
                break
            time.sleep(.005)
    assert len(backend.calls) == 1 and worker.status()["pending"] == 1


def test_startup_blocked_get_included_in_stop_budget(prepared):
    stop, entered, release, ended = runtime.VectorWorkerStop(), threading.Event(), threading.Event(), threading.Event()
    def blocked():
        entered.set()
        release.wait(2)
        ended.set()
    prepared[3]._require_sync_ready = blocked
    requester = threading.Thread(target=lambda: (entered.wait(2), stop.request_stop()))
    requester.start()
    try:
        result = watch(prepared, stop=stop, policy=VectorWorkerPolicy(32, .1, .1))
        assert result.reason == "shutdown_timeout" and prepared[4].status() == {}
        assert result.diagnostics_status == "not_observed" and result.diagnostics is None
    finally:
        release.set()
        requester.join(2)
        assert ended.wait(2)


def test_unexpected_ack_failure_loop_failed_preserves_claim(prepared, monkeypatch):
    seed(prepared)
    monkeypatch.setattr(MemoryVectorOutbox, "_finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic-private")))
    result = watch(prepared, stop=runtime.VectorWorkerStop())
    assert result.reason == "worker_loop_failed" and result.exit_code == 1
    assert prepared[4].status()["processing"] == 1 and len(prepared[3].calls) == 1


def test_preflight_failure_is_safe_no_thread_traceback(prepared, capsys):
    prepared[3]._require_sync_ready = lambda: (_ for _ in ()).throw(RuntimeError("synthetic-private"))
    result = watch(prepared, stop=runtime.VectorWorkerStop())
    assert result.reason == "startup_failed" and result.exit_code == 1
    assert "private" not in repr(result) + capsys.readouterr().err


@pytest.mark.parametrize("count", [True, -1, 2, None])
def test_invalid_single_event_count_is_failure(prepared, monkeypatch, count):
    monkeypatch.setattr(MemoryVectorOutbox, "run_once", lambda *a, **k: count)
    assert watch(prepared, stop=runtime.VectorWorkerStop()).reason == "worker_loop_failed"


def test_repeated_stop_does_not_extend_deadline():
    clock = [100.]
    stop = runtime.VectorWorkerStop(clock=lambda: clock[0])
    stop.request_stop()
    first = stop.observe(10)
    clock[0] = 105.
    stop.request_stop()
    assert stop.observe(10) is first and first.remaining_seconds(clock[0]) == 5


def test_signal_handlers_record_only_restore_on_exception(monkeypatch):
    original = {signal.SIGINT: object(), signal.SIGTERM: object()}
    handlers = dict(original)
    monkeypatch.setattr(runtime.signal, "getsignal", lambda n: handlers[n])
    monkeypatch.setattr(runtime.signal, "signal", lambda n, f: handlers.__setitem__(n, f))
    stop = runtime.VectorWorkerStop()
    with pytest.raises(RuntimeError), runtime.vector_worker_signals(stop):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert stop.requested and not stop.wake.is_set() and stop.window is None
        raise RuntimeError("fixture")
    assert handlers == original


def cli_args(prepared):
    return ["--database", str(prepared[0].database.path), "--model-fingerprint", "model-v1", "--dimensions", "2"]


@pytest.mark.parametrize("args", [["--watch", "--run"], ["--watch", "--status"],
    ["--watch", "--initialize-target"], ["--watch", "--retry-dead"], ["--watch", "--limit", "1"], ["--watch"]])
def test_watch_invalid_actions_rejected_before_settings(monkeypatch, args):
    monkeypatch.setattr(vector_sync, "Settings", lambda: pytest.fail("invalid CLI opened settings"))
    with pytest.raises(SystemExit) as caught:
        vector_sync.main(args)
    assert caught.value.code == 2


def test_disabled_watch_never_constructs_backend_or_starts_thread(prepared, monkeypatch, capsys):
    monkeypatch.setattr(vector_sync, "QdrantVersionedVectorBackend", lambda *a: pytest.fail("disabled constructed backend"))
    with pytest.raises(SystemExit) as caught:
        vector_sync.main(["--watch", *cli_args(prepared)])
    assert caught.value.code == 1 and "ValueError" in capsys.readouterr().err


@pytest.mark.parametrize("reason,code", [("stopped", 0), ("startup_failed", 1), ("shutdown_timeout", 1)])
def test_cli_watch_exit_categories_and_no_secrets(prepared, monkeypatch, capsys, reason, code):
    monkeypatch.setattr(vector_sync, "Settings", lambda: Settings(_env_file=None, memory_vector_worker_enabled=True))
    monkeypatch.setattr(vector_sync, "QdrantVersionedVectorBackend", lambda *a: prepared[3])
    monkeypatch.setattr(vector_sync, "vector_worker_signals", lambda stop: nullcontext())
    monkeypatch.setattr(vector_sync, "watch_vector_worker", lambda *a, **k: runtime.VectorWorkerResult(reason, 2))
    if code:
        with pytest.raises(SystemExit) as caught:
            vector_sync.main(["--watch", *cli_args(prepared)])
        assert caught.value.code == code
    else:
        vector_sync.main(["--watch", *cli_args(prepared)])
    output = capsys.readouterr()
    assert reason in output.out and '"embedding_calls": 0' in output.out
    assert str(prepared[0].database.path) not in output.out and "model-v1" not in output.out


def test_plan_still_opens_nothing(monkeypatch, capsys):
    monkeypatch.setattr(vector_sync, "Settings", lambda: pytest.fail("plan opened config"))
    vector_sync.main([])
    assert "Plan only" in capsys.readouterr().out


def test_existing_one_shot_run_limit_still_works(prepared, monkeypatch):
    seed(prepared, 2)
    monkeypatch.setattr(vector_sync, "QdrantVersionedVectorBackend", lambda *a: prepared[3])
    vector_sync.main(["--run", "--limit", "1", *cli_args(prepared)])
    assert prepared[4].status()["pending"] == 1 and len(prepared[3].calls) == 1


def test_worker_database_missing_never_creates_directory_or_empty_file(tmp_path):
    import sqlite3
    path = tmp_path / "missing" / "worker.sqlite3"
    database = runtime._ExistingWorkerDatabase(path)
    with pytest.raises(sqlite3.OperationalError), database.connect():
        pytest.fail("missing worker DB opened")
    assert not path.parent.exists()


def test_worker_database_existing_connection_uses_rw_uri(prepared, monkeypatch):
    import sqlite3
    original, seen = sqlite3.connect, []
    def connect(database_uri, **kwargs):
        seen.append((database_uri, kwargs))
        return original(database_uri, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with runtime._ExistingWorkerDatabase(prepared[0].database.path).connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert len(seen) == 1 and seen[0][0].endswith("?mode=rw") and seen[0][1]["uri"]


def test_signal_partial_install_failure_restores_first_handler(monkeypatch):
    originals = {signal.SIGINT: object(), signal.SIGTERM: object()}
    installed = dict(originals)
    monkeypatch.setattr(runtime.signal, "getsignal", lambda n: originals[n])
    def set_handler(n, value):
        if n == signal.SIGTERM:
            raise RuntimeError("fixture install failure")
        installed[n] = value
    monkeypatch.setattr(runtime.signal, "signal", set_handler)
    with pytest.raises(RuntimeError), runtime.vector_worker_signals(runtime.VectorWorkerStop()):
        pytest.fail("partially installed context entered")
    assert installed == originals


def test_non_main_thread_signal_context_refused():
    reasons = []
    def attempt():
        try:
            with runtime.vector_worker_signals(runtime.VectorWorkerStop()):
                reasons.append("entered")
        except RuntimeError:
            reasons.append("refused")
    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and reasons == ["refused"]


@pytest.mark.parametrize("callback", [True, "stop", 1])
def test_claim_invalid_stop_callback_rejected(prepared, callback):
    with pytest.raises(TypeError):
        prepared[4].claim(should_stop=callback)
