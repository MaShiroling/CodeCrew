"""Contracts/preflight only; no worker loop, provider, process or real data."""

import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorModel
from app.chat.vector_worker_contracts import (
    PreparedVectorWorkerTarget,
    VectorWorkerPolicy,
    VectorWorkerStartError,
    VectorWorkerStopRequest,
    check_vector_worker_target,
    requested_vector_worker_policy,
)
from app.config import Settings
from app.storage import SQLiteDatabase


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    import os
    import urllib.request
    for name in tuple(os.environ):
        if name.startswith("CODECREW_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("unexpected network"))


def settings(**values):
    return Settings(_env_file=None, **values)


class ReadOnlyBackend:
    model = VectorModel("synthetic-worker-v1", 2)
    sync_target_id = "a" * 64

    def __init__(self):
        self.checks, self.fail, self.hook = 0, False, None

    def _require_sync_ready(self):
        self.checks += 1
        if self.hook:
            self.hook()
        if self.fail:
            raise RuntimeError("synthetic-private-provider-body-key")

    def initialize(self):
        pytest.fail("probe initialized remote")

    def apply_mutation(self, mutation):
        pytest.fail("probe delivered mutation")


@pytest.fixture
def prepared(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "worker ?# 数据.sqlite3"))
    store.initialize()
    backend = ReadOnlyBackend()
    with store.database.transaction() as conn:
        conn.execute("INSERT INTO standalone_chat_vector_targets VALUES (?, ?, ?)",
                     (backend.sync_target_id, backend.model.fingerprint, backend.model.dimensions))
    return store, backend


def snapshot(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return tuple(conn.iterdump())
    finally:
        conn.close()


def test_defaults_settings_and_enabled_alone_never_prepare(monkeypatch):
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("settings opened DB"))
    s = settings()
    assert s.memory_vector_store == "sqlite" and s.memory_embedding_mode == "off"
    assert s.memory_vector_worker_enabled is False
    assert (s.memory_vector_worker_batch_size, s.memory_vector_worker_poll_interval_seconds,
            s.memory_vector_worker_shutdown_grace_seconds) == (32, 2., 10.)
    assert requested_vector_worker_policy(s) is None
    assert requested_vector_worker_policy(settings(memory_vector_worker_enabled=True)) is None


def test_nonrequested_gate_is_lazy():
    class Unreadable:
        def __getattribute__(self, name):
            pytest.fail("nonrequested gate accessed settings")
    assert requested_vector_worker_policy(Unreadable()) is None


def test_requested_disabled_fails_and_enabled_independent_of_chat_cloud():
    with pytest.raises(ValueError, match="WORKER_ENABLED"):
        requested_vector_worker_policy(settings(), requested=True)
    assert requested_vector_worker_policy(settings(memory_vector_worker_enabled=True), requested=True) == VectorWorkerPolicy()


@pytest.mark.parametrize("requested", [1, 0, "true", None])
def test_request_must_be_typed_bool(requested):
    with pytest.raises(ValueError):
        requested_vector_worker_policy(settings(), requested=requested)


def test_environment_numeric_strings_and_enable(monkeypatch):
    for name, value in {"ENABLED": "true", "BATCH_SIZE": "17", "POLL_INTERVAL_SECONDS": ".5",
                        "SHUTDOWN_GRACE_SECONDS": "20"}.items():
        monkeypatch.setenv("CODECREW_MEMORY_VECTOR_WORKER_" + name, value)
    assert requested_vector_worker_policy(settings(), requested=True) == VectorWorkerPolicy(17, .5, 20.)


@pytest.mark.parametrize("value", [0, -1, 201, 3.5, True, False, "bad", "NaN"])
def test_settings_batch_bounds(value):
    with pytest.raises(ValidationError):
        settings(memory_vector_worker_batch_size=value)


@pytest.mark.parametrize("name", ["poll_interval_seconds", "shutdown_grace_seconds"])
@pytest.mark.parametrize("value", [0, -.1, .01, 61, True, False, float("nan"), float("inf"), "NaN", "Infinity", "bad"])
def test_settings_timings_finite_bounded_not_bool(name, value):
    with pytest.raises(ValidationError):
        settings(**{"memory_vector_worker_" + name: value})


@pytest.mark.parametrize("policy", [VectorWorkerPolicy(1, .1, .1), VectorWorkerPolicy(200, 60, 60)])
def test_policy_valid_boundaries_and_immutable(policy):
    with pytest.raises(FrozenInstanceError):
        policy.batch_size = 1


@pytest.mark.parametrize("values", [{"batch_size": True}, {"batch_size": 1.}, {"batch_size": 201},
    {"poll_interval_seconds": True}, {"poll_interval_seconds": float("nan")},
    {"shutdown_grace_seconds": float("inf")}, {"shutdown_grace_seconds": 0}])
def test_direct_policy_strict(values):
    with pytest.raises(ValueError):
        VectorWorkerPolicy(**values)


def test_stop_window_deadline_and_immutable():
    stop = VectorWorkerStopRequest(100., 10.)
    assert [stop.remaining_seconds(t) for t in (100., 105., 110., 111.)] == [10, 5, 0, 0]
    with pytest.raises(FrozenInstanceError):
        stop.requested_at = 105.


@pytest.mark.parametrize("requested,grace", [(True, 10), (-1, 10), (float("nan"), 10),
    (1, False), (1, 0), (1, 61), (1, float("inf"))])
def test_invalid_stop_window(requested, grace):
    with pytest.raises(ValueError):
        VectorWorkerStopRequest(requested, grace)


@pytest.mark.parametrize("now", [99., True, float("nan"), float("inf")])
def test_stop_clock_never_backwards(now):
    with pytest.raises(ValueError):
        VectorWorkerStopRequest(100., 10.).remaining_seconds(now)


def test_preflight_read_only_without_cached_model_or_claim(prepared):
    store, backend = prepared
    before = snapshot(store.database.path)
    target = check_vector_worker_target(store.database.path, backend)
    assert target == PreparedVectorWorkerTarget(store.database.path, backend.model, backend.sync_target_id)
    assert backend.checks == 1 and before == snapshot(store.database.path)
    assert str(store.database.path) not in repr(target)
    with pytest.raises(FrozenInstanceError):
        target.target_id = "b" * 64


def test_connection_closed_before_remote(prepared):
    store, backend = prepared
    def exclusive():
        conn = sqlite3.connect(store.database.path, timeout=0)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("BEGIN EXCLUSIVE")
            conn.rollback()
        finally:
            conn.close()
    backend.hook = exclusive
    check_vector_worker_target(store.database.path, backend)


@pytest.mark.parametrize("statement,category", [
    ("DELETE FROM schema_migrations WHERE version=27", "outbox_schema_not_prepared"),
    ("UPDATE schema_migrations SET checksum='synthetic' WHERE version=27", "outbox_schema_not_prepared"),
    ("DELETE FROM standalone_chat_vector_targets", "target_not_prepared"),
    ("UPDATE standalone_chat_vector_targets SET model_fingerprint='other'", "target_not_prepared"),
    ("UPDATE standalone_chat_vector_targets SET dimensions=3", "target_not_prepared"),
    ("INSERT INTO standalone_chat_embedding_meta VALUES ('synthetic-worker-v1',3)", "cache_model_mismatch"),
    ("DROP TABLE standalone_chat_vector_outbox", "database_schema_unavailable"),
    ("DROP TABLE standalone_chat_embedding_meta", "database_schema_unavailable")])
def test_local_failure_never_probes_or_repairs(prepared, statement, category):
    store, backend = prepared
    with store.database.transaction() as conn:
        conn.execute(statement)
    before = snapshot(store.database.path)
    with pytest.raises(VectorWorkerStartError, match=category):
        check_vector_worker_target(store.database.path, backend)
    assert backend.checks == 0 and before == snapshot(store.database.path)


def test_registered_cache_model_match(prepared):
    store, backend = prepared
    with store.database.transaction() as conn:
        conn.execute("INSERT INTO standalone_chat_embedding_meta VALUES (?, ?)", (backend.model.fingerprint, 2))
    check_vector_worker_target(store.database.path, backend)
    assert backend.checks == 1


def test_remote_failure_safe_next_probe_fresh(prepared):
    store, backend = prepared
    before = snapshot(store.database.path)
    backend.fail = True
    with pytest.raises(VectorWorkerStartError) as caught:
        check_vector_worker_target(store.database.path, backend)
    assert str(caught.value) == "remote_not_ready" and caught.value.__cause__ is None
    assert before == snapshot(store.database.path)
    backend.fail = False
    check_vector_worker_target(store.database.path, backend)
    assert backend.checks == 2


def test_absent_database_not_created_and_directory_rejected(tmp_path):
    path = tmp_path / "missing" / "db.sqlite3"
    backend = ReadOnlyBackend()
    with pytest.raises(VectorWorkerStartError, match="database_unavailable"):
        check_vector_worker_target(path, backend)
    assert not path.parent.exists() and backend.checks == 0
    with pytest.raises(VectorWorkerStartError, match="database_not_file"):
        check_vector_worker_target(tmp_path, backend)


@pytest.mark.parametrize("target", ["secret", "A" * 64, "a" * 63, True])
def test_invalid_target_before_database_open(tmp_path, target, monkeypatch):
    backend = ReadOnlyBackend()
    backend.sync_target_id = target
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("invalid target opened DB"))
    with pytest.raises(ValueError):
        check_vector_worker_target(tmp_path / "absent", backend)


def test_probe_uses_read_only_escaped_uri(prepared, monkeypatch):
    store, backend = prepared
    original, seen = sqlite3.connect, []
    def checked(database_uri, **kwargs):
        seen.append((database_uri, kwargs))
        return original(database_uri, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", checked)
    check_vector_worker_target(store.database.path, backend)
    assert len(seen) == 1 and seen[0][1]["uri"] is True
    assert seen[0][0].endswith("?mode=ro") and "%3F%23" in seen[0][0]


def test_preflight_never_claims_resets_retries_or_changes_existing_events(prepared):
    store, backend = prepared
    with store.database.transaction() as conn:
        for i, status in enumerate(("pending", "processing", "waiting", "dead"), start=1):
            conn.execute("INSERT INTO standalone_chat_vector_revisions VALUES (?, ?, ?, ?)",
                         (str(i), "synthetic-room", 1, 0))
            conn.execute("INSERT INTO standalone_chat_vector_outbox "
                         "(target_id,memory_id,revision,status,attempts,lease_token,lease_until,error_code) "
                         "VALUES (?, ?, 1, ?, 2, ?, 9999999999, ?)",
                         (backend.sync_target_id, str(i), status, "b" * 32, "synthetic_failure"))
    before = snapshot(store.database.path)
    check_vector_worker_target(store.database.path, backend)
    assert before == snapshot(store.database.path)


@pytest.mark.parametrize("missing_revision_index", [False, True])
def test_real_adapter_probe_only_one_metadata_get_no_init_or_delivery(prepared, missing_revision_index):
    from app.chat.qdrant_vector_backend import PAYLOAD_SCHEMA, QdrantEndpoint
    from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
    from tests.test_qdrant_vector_sync import ConditionalREST
    store, _ = prepared
    backend = QdrantVersionedVectorBackend(QdrantEndpoint(), ReadOnlyBackend.model)
    rest = ConditionalREST(backend.model)
    # Only fixture data setup: don't call remote initializer even with fake HTTP.
    rest.exists = True
    rest.schema = {key: {"data_type": kind} for key, kind in PAYLOAD_SCHEMA.items()}
    if not missing_revision_index:
        rest.schema["sync_revision"] = {"data_type": "integer"}
    backend._opener = rest
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_vector_targets SET target_id=?", (backend.sync_target_id,))
    before = snapshot(store.database.path)
    if missing_revision_index:
        with pytest.raises(VectorWorkerStartError, match="remote_not_ready"):
            check_vector_worker_target(store.database.path, backend)
    else:
        check_vector_worker_target(store.database.path, backend)
    assert [method for method, *_ in rest.requests] == ["GET"]
    assert before == snapshot(store.database.path) and not rest.points


@pytest.mark.parametrize("path,model,target", [(Path("relative"), VectorModel("model", 2), "a" * 64),
    ("/absolute", VectorModel("model", 2), "a" * 64), (Path("/absolute"), "model", "a" * 64),
    (Path("/absolute"), VectorModel("model", 2), "secret")])
def test_prepared_target_typed(path, model, target):
    with pytest.raises((TypeError, ValueError)):
        PreparedVectorWorkerTarget(path, model, target)
