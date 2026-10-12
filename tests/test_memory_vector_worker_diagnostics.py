"""Metadata-only, transactional observations; fake fixtures are not live SLA."""

import json
import sqlite3
import time
from uuid import uuid4

import pytest

from app.chat import vector_sync, vector_worker
from app.chat.memory import CHAT_MEMORY_MIGRATIONS
from app.chat.service import StandaloneChatService
from app.chat.store import STANDALONE_CHAT_MIGRATIONS, StandaloneChatStore
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import VectorWorkerPolicy
from app.chat.vector_worker_diagnostics import (
    read_vector_worker_diagnostics,
    safe_worker_diagnostics,
)
from app.chat.vector_worker_telemetry_schema import VECTOR_WORKER_TELEMETRY_MIGRATION as MIGRATION
from app.config import Settings
from app.storage import SQLiteDatabase
from tests.test_memory_vector_outbox import VersionedFake, cache, save
from tests.test_memory_vector_worker_contracts import snapshot


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected provider"))
    monkeypatch.setattr(vector_sync, "Settings", lambda: Settings(_env_file=None))


@pytest.fixture
def prepared(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "diagnostics ?# 数据.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="diagnostics", idempotency_key=uuid4()).room_id
    backend = VersionedFake()
    backend._require_sync_ready = lambda: None
    clock = [100.]
    worker = MemoryVectorOutbox(store.database, backend, clock=lambda: clock[0])
    worker.register_target()
    return store, service, room, backend, clock, worker


def diagnostic(prepared, *, now=100):
    return read_vector_worker_diagnostics(prepared[0].database.path, prepared[-1].target_id, now=now)


def event(conn, target, status="pending", *, enqueued=90, available=0, lease_until=None, error=None):
    memory = str(uuid4())
    conn.execute("INSERT INTO standalone_chat_vector_revisions VALUES (?, ?, 1, 0)", (memory, str(uuid4())))
    cursor = conn.execute("INSERT INTO standalone_chat_vector_outbox "
        "(target_id,memory_id,revision,status,available_at,lease_until,error_code) VALUES (?, ?, 1, ?, ?, ?, ?)",
        (target, memory, status, available, lease_until, error))
    if enqueued is not None:
        conn.execute("UPDATE standalone_chat_vector_event_times SET enqueued_at=? WHERE event_id=?", (enqueued, cursor.lastrowid))
    return cursor.lastrowid


def test_empty_registered_is_not_remote_health_assertion(prepared):
    value = diagnostic(prepared)
    assert value["target_registered"] and value["timing_metadata_available"]
    assert value["backlog_total"] == value["backlog_age_unknown_count"] == 0
    assert value["oldest_known_backlog_age_seconds"] is value["last_delivered_at_unix"] is None
    assert "healthy" not in value


def test_backlog_ready_retry_waiting_dead_expired_and_known_age(prepared):
    store, *_, worker = prepared
    with store.database.transaction() as conn:
        event(conn, worker.target_id, enqueued=30, available=90)
        event(conn, worker.target_id, enqueued=40, available=200, error="delivery_failed")
        event(conn, worker.target_id, "processing", enqueued=50, lease_until=99)
        event(conn, worker.target_id, "waiting", enqueued=60, error="vector_missing_or_stale")
        event(conn, worker.target_id, "dead", enqueued=70, error="lease_exhausted")
    value = diagnostic(prepared)
    assert value["backlog_total"] == value["backlog_age_known_count"] == 5
    assert value["status_counts"]["pending"] == 2 and value["ready_pending_count"] == 1
    assert value["expired_processing_count"] == 1
    assert value["oldest_known_backlog_age_seconds"] == 70
    assert value["current_error_counts"] == {"delivery_failed": 1, "lease_exhausted": 1, "vector_missing_or_stale": 1}


def test_partial_unknown_does_not_claim_full_backlog_age(prepared):
    store, *_, worker = prepared
    with store.database.transaction() as conn:
        known = event(conn, worker.target_id, enqueued=80)
        unknown = event(conn, worker.target_id)
        conn.execute("DELETE FROM standalone_chat_vector_event_times WHERE event_id=?", (unknown,))
    value = diagnostic(prepared)
    assert known != unknown and value["backlog_age_known_count"] == value["backlog_age_unknown_count"] == 1
    assert value["oldest_known_backlog_age_seconds"] == 20


def test_target_isolation_and_unregistered_visibility(prepared):
    store, *_, _worker = prepared
    with store.database.transaction() as conn:
        conn.execute("INSERT INTO standalone_chat_vector_targets VALUES (?, 'other', 2)", ("b" * 64,))
        event(conn, "b" * 64, "dead", enqueued=1, error="delivery_failed")
    assert diagnostic(prepared)["backlog_total"] == 0
    other = read_vector_worker_diagnostics(store.database.path, "b" * 64, now=100)
    assert other["backlog_total"] == 1 and other["oldest_known_backlog_age_seconds"] == 99
    assert not read_vector_worker_diagnostics(store.database.path, "c" * 64)["target_registered"]


def test_unknown_error_text_is_mapped_in_sql_not_logged(prepared):
    store, *_, worker = prepared
    secret = "synthetic-sk-private-provider-body"
    with store.database.transaction() as conn:
        event(conn, worker.target_id, "dead", error=secret)
        event(conn, worker.target_id, "superseded", error="delivery_failed")
    value = diagnostic(prepared)
    assert value["current_error_counts"] == {"other": 1}
    assert secret not in json.dumps(value) and "source_message" not in json.dumps(value)


@pytest.mark.parametrize("timestamp", [101, float("inf")])
def test_future_or_invalid_age_not_silently_negative_or_zero(prepared, timestamp):
    with prepared[0].database.transaction() as conn:
        event(conn, prepared[-1].target_id, enqueued=timestamp)
    value = diagnostic(prepared)
    assert value["backlog_clock_skew_count"] == value["backlog_age_unknown_count"] == 1
    assert value["oldest_known_backlog_age_seconds"] is None


def test_recent_success_survives_new_worker_instance(prepared):
    store, service, room, backend, _, worker = prepared
    cache(store, save(service, room))
    assert worker.run_once() == 1
    first = diagnostic(prepared, now=time.time()+1)
    restarted = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend)
    assert restarted.diagnostics()["last_delivered_at_unix"] == first["last_delivered_at_unix"]
    assert first["last_delivered_at_unix"] is not None and first["delivery_time_unknown_count"] == 0
    assert first["seconds_since_last_delivery"] == first["observed_at_unix"]-first["last_delivered_at_unix"]


def test_waiting_remote_tombstone_ack_is_not_delivered_success(prepared):
    _store, service, room, _, _, worker = prepared
    save(service, room)
    worker.run_once()
    value = diagnostic(prepared, now=time.time()+1)
    assert value["status_counts"]["waiting"] == 1 and value["last_delivered_at_unix"] is None


def test_superseded_ack_does_not_record_success(prepared):
    store, service, room, backend, _, worker = prepared
    memory = save(service, room)
    cache(store, memory)
    backend.after_apply = lambda: store.memory.retire(room, memory.memory_id)
    worker.run_once(limit=1)
    assert diagnostic(prepared, now=time.time()+1)["last_delivered_at_unix"] is None


def test_lost_lease_ack_never_sets_last_success(prepared):
    store, service, room, _, clock, worker = prepared
    cache(store, save(service, room))
    old = worker.claim()
    clock[0] = 161
    new = worker.claim()
    assert new and old and new.token != old.token
    assert not worker._finish(old, "delivered")
    assert diagnostic(prepared, now=time.time()+1)["last_delivered_at_unix"] is None
    worker.deliver(new)
    assert diagnostic(prepared, now=time.time()+1)["last_delivered_at_unix"] is not None


def test_telemetry_rolls_back_with_enqueue_and_ack(prepared):
    store, *_, worker = prepared
    before = snapshot(store.database.path)
    with pytest.raises(RuntimeError), store.database.transaction() as conn:
        identifier = event(conn, worker.target_id)
        assert conn.execute("SELECT enqueued_at FROM standalone_chat_vector_event_times WHERE event_id=?", (identifier,)).fetchone()
        raise RuntimeError("rollback fixture")
    assert snapshot(store.database.path) == before
    with store.database.transaction() as conn:
        identifier = event(conn, worker.target_id, "processing")
    with pytest.raises(RuntimeError), store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_vector_outbox SET status='delivered' WHERE event_id=?", (identifier,))
        assert conn.execute("SELECT delivered_at FROM standalone_chat_vector_event_times WHERE event_id=?", (identifier,)).fetchone()[0]
        raise RuntimeError("rollback fixture")
    assert diagnostic(prepared, now=time.time()+1)["last_delivered_at_unix"] is None


def test_legacy_upgrade_no_invented_enqueue_or_delivery_time(tmp_path):
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    database.initialize((*STANDALONE_CHAT_MIGRATIONS, *CHAT_MEMORY_MIGRATIONS))
    worker = MemoryVectorOutbox(database, VersionedFake())
    worker.register_target()
    with database.transaction() as conn:
        event(conn, worker.target_id, enqueued=None)
    old = read_vector_worker_diagnostics(database.path, worker.target_id, now=100)
    assert not old["timing_metadata_available"] and old["backlog_age_unknown_count"] == 1
    database.apply_migrations((MIGRATION,))
    database.apply_migrations((MIGRATION,))
    new = read_vector_worker_diagnostics(database.path, worker.target_id, now=100)
    assert new["timing_metadata_available"] and new["backlog_age_unknown_count"] == 1
    assert new["oldest_known_backlog_age_seconds"] is None
    with database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_vector_outbox SET status='delivered'")
    value = read_vector_worker_diagnostics(database.path, worker.target_id, now=time.time()+1)
    assert value["last_delivered_at_unix"] is not None and value["backlog_total"] == 0
    with database.connect() as conn:
        assert conn.execute("SELECT enqueued_at FROM standalone_chat_vector_event_times").fetchone()[0] is None


def test_redrive_and_retry_availability_do_not_change_enqueue_time(prepared):
    store, *_, worker = prepared
    with store.database.transaction() as conn:
        identifier = event(conn, worker.target_id, "dead", enqueued=10, available=200, error="delivery_failed")
    before = diagnostic(prepared)["oldest_known_backlog_age_seconds"]
    assert worker.retry_dead() == 1
    assert diagnostic(prepared)["oldest_known_backlog_age_seconds"] == before == 90
    with store.database.connect() as conn:
        assert conn.execute("SELECT enqueued_at FROM standalone_chat_vector_event_times WHERE event_id=?", (identifier,)).fetchone()[0] == 10


def test_read_only_snapshot_does_not_select_fact_payloads(prepared, monkeypatch):
    original, statements = sqlite3.connect, []
    before = snapshot(prepared[0].database.path)
    def connect(database_uri, **kwargs):
        assert kwargs.get("uri") and database_uri.endswith("?mode=ro")
        conn = original(database_uri, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        diagnostic(prepared)
    assert snapshot(prepared[0].database.path) == before
    forbidden = ("standalone_chat_memories", "standalone_chat_messages", "standalone_chat_memory_embeddings", "lease_token")
    assert not any(word in sql for word in forbidden for sql in statements)


@pytest.mark.parametrize("now", [True, -1, float("nan"), float("inf"), "100"])
def test_invalid_observation_clock_rejected(prepared, now):
    with pytest.raises(ValueError):
        diagnostic(prepared, now=now)


def test_absent_read_database_not_created(tmp_path):
    path = tmp_path / "absent" / "diag.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        read_vector_worker_diagnostics(path, "a" * 64)
    assert not path.parent.exists()


def test_future_success_exposes_unknown_and_clock_skew(prepared):
    with prepared[0].database.transaction() as conn:
        identifier = event(conn, prepared[-1].target_id)
        conn.execute("UPDATE standalone_chat_vector_outbox SET status='delivered' WHERE event_id=?", (identifier,))
        conn.execute("UPDATE standalone_chat_vector_event_times SET delivered_at=200 WHERE event_id=?", (identifier,))
    value = diagnostic(prepared)
    assert value["last_delivered_at_unix"] is None and value["delivery_time_unknown_count"] == value["delivery_clock_skew_count"] == 1


def test_optional_diagnostics_failure_does_not_fail_watch_delivery(prepared, monkeypatch):
    store, service, room, backend, _, worker = prepared
    cache(store, save(service, room))
    monkeypatch.setattr(MemoryVectorOutbox, "diagnostics", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic-secret")))
    stop = vector_worker.VectorWorkerStop()
    backend.after_apply = stop.request_stop
    result = vector_worker.watch_vector_worker(store.database.path, backend, VectorWorkerPolicy(32, .1, 1), stop=stop)
    assert result.exit_code == 0 and result.diagnostics_status == "unavailable" and result.diagnostics is None
    assert worker.status()["delivered"] == 1 and "secret" not in repr(result)


def test_watch_result_contains_final_persisted_success(prepared):
    store, service, room, backend, _, _ = prepared
    cache(store, save(service, room))
    stop = vector_worker.VectorWorkerStop()
    backend.after_apply = stop.request_stop
    result = vector_worker.watch_vector_worker(store.database.path, backend, VectorWorkerPolicy(32, .1, 1), stop=stop)
    assert result.exit_code == 0 and result.diagnostics_status == "observed"
    assert result.diagnostics["last_delivered_at_unix"] is not None


def test_safe_diagnostics_masks_exceptions():
    class Broken:
        def diagnostics(self):
            raise RuntimeError("synthetic-private-provider-body")
    assert safe_worker_diagnostics(Broken()) == {"diagnostics": None, "diagnostics_status": "unavailable"}


def test_status_cli_includes_safe_diagnostics_and_no_network(prepared, monkeypatch, capsys):
    store, *_, worker = prepared
    monkeypatch.setattr(vector_sync, "QdrantVersionedVectorBackend", lambda *a: prepared[3])
    vector_sync.main(["--status", "--database", str(store.database.path), "--model-fingerprint", "model-v1", "--dimensions", "2"])
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics_status"] == "observed" and output["diagnostics"]["target_registered"]
    assert output["status"] == worker.status()
    assert str(store.database.path) not in json.dumps(output) and "model-v1" not in json.dumps(output)


def test_committed_one_shot_still_success_if_diagnostics_unavailable(prepared, monkeypatch, capsys):
    store, service, room, backend, _, worker = prepared
    cache(store, save(service, room))
    monkeypatch.setattr(vector_sync, "QdrantVersionedVectorBackend", lambda *a: backend)
    monkeypatch.setattr(MemoryVectorOutbox, "diagnostics", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic-secret")))
    vector_sync.main(["--run", "--database", str(store.database.path), "--model-fingerprint", "model-v1", "--dimensions", "2"])
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics_status"] == "unavailable" and output["attempted"] == 1
    assert worker.status()["delivered"] == 1 and "secret" not in json.dumps(output)


def test_telemetry_write_failure_rolls_back_fact_and_outbox_together(prepared):
    store, service, room, *_ = prepared
    source = service.post_message(room, content="@白金 合成事务", idempotency_key=uuid4(), reply_to=None).message
    with store.database.transaction() as conn:
        conn.execute("CREATE TRIGGER synthetic_time_failure BEFORE INSERT ON standalone_chat_vector_event_times "
                     "BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    before = snapshot(store.database.path)
    with pytest.raises(sqlite3.IntegrityError):
        service.create_memory(room, source_message_id=source.message_id, content="合成事务")
    assert snapshot(store.database.path) == before and store.memory.list_active(room) == ()


def test_time_write_failure_after_remote_commit_preserves_unknown_claim(prepared):
    store, service, room, backend, _, worker = prepared
    cache(store, save(service, room))
    with store.database.transaction() as conn:
        conn.execute("CREATE TRIGGER synthetic_ack_time_failure BEFORE UPDATE ON standalone_chat_vector_event_times "
                     "BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    result = vector_worker.watch_vector_worker(store.database.path, backend, VectorWorkerPolicy(32, .1, 1))
    assert result.reason == "worker_loop_failed" and worker.status()["processing"] == 1
    assert backend.points and diagnostic(prepared, now=time.time()+1)["last_delivered_at_unix"] is None
