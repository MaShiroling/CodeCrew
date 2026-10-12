"""Transactional outbox/crash fencing; fake storage is not real Qdrant proof."""

import hashlib
import json
import sqlite3
import struct
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from app.chat.memory import AgentMemoryProposal
from app.chat.service import StandaloneChatService
from app.chat.store import STANDALONE_CHAT_MIGRATIONS, StandaloneChatStore
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord
from app.chat.vector_outbox import MemoryVectorOutbox, SyncPolicy
from app.chat.vector_sync_contracts import MAX_SYNC_REVISION, VectorMutation
from app.storage import SQLiteDatabase


class VersionedFake:
    model = VectorModel("model-v1", 2)
    sync_target_id = "a" * 64

    def __init__(self):
        self.points, self.calls = {}, []
        self.fail = False
        self.after_apply = None

    def apply_mutation(self, mutation):
        self.calls.append(mutation)
        if self.fail:
            raise RuntimeError("secret-provider-body")
        key = (mutation.room_id, mutation.memory_id)
        old = self.points.get(key)
        if old is None or old.revision < mutation.revision:
            self.points[key] = mutation
        elif old.revision == mutation.revision and old != mutation:
            raise ValueError("same revision conflict")
        if self.after_apply:
            self.after_apply()
        return self.points[key].revision


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda *a, **k: pytest.fail("outbox called network/model"))
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "outbox.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="outbox", idempotency_key=uuid4()).room_id
    backend = VersionedFake()
    clock = [100.0]
    worker = MemoryVectorOutbox(store.database, backend, clock=lambda: clock[0])
    worker.register_target()
    return store, service, room, backend, clock, worker


def save(service, room, text="原事实", **kwargs):
    source = service.post_message(room, content="@白金 " + text,
                                  idempotency_key=uuid4(), reply_to=None).message
    return service.create_memory(room, source_message_id=source.message_id, content=text, **kwargs)


def cache(store, memory, *, model="model-v1", vector=(1.0, 0.0), content_hash=None):
    with store.database.transaction() as conn:
        conn.execute("INSERT INTO standalone_chat_memory_embeddings VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(memory_id) DO UPDATE SET model_fingerprint=excluded.model_fingerprint, "
                     "content_hash=excluded.content_hash, dimensions=excluded.dimensions, vector=excluded.vector",
                     (str(memory.memory_id), model, content_hash or hashlib.sha256(memory.content.encode()).hexdigest(),
                      len(vector), struct.pack(f"<{len(vector)}f", *vector)))


def rows(store):
    with store.database.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM standalone_chat_vector_outbox ORDER BY event_id")]


def test_future_subscription_default_and_atomic_rollback(setup):
    store, service, room, backend, _, worker = setup
    assert worker.status() == {}
    memory = save(service, room)
    assert rows(store)[0]["revision"] == 1
    with pytest.raises(RuntimeError), store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET content='回滚内容' WHERE memory_id=?",
                     (str(memory.memory_id),))
        assert conn.execute("SELECT count(*) FROM standalone_chat_vector_outbox").fetchone()[0] == 2
        raise RuntimeError("rollback")
    assert len(rows(store)) == 1
    assert store.memory.list_active(room)[0].content == memory.content
    assert backend.calls == [] and store.memory.vector_index is None


def test_outbox_insert_failure_rolls_back_fact_change(setup):
    store, service, room, _, _, _ = setup
    memory = save(service, room)
    with store.database.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_enqueue BEFORE INSERT ON standalone_chat_vector_outbox "
                     "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.memory.retire(room, memory.memory_id)
    assert store.memory.list_active(room) == (memory,)
    assert len(rows(store)) == 1


def test_cached_write_coalesces_versions_and_no_network_inside_transaction(setup):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    cache(store, memory)
    # A second writer must be able to commit from inside the network callback.
    def concurrent_write():
        with SQLiteDatabase(store.database.path, busy_timeout_ms=100).transaction() as conn:
            conn.execute("UPDATE standalone_chat_rooms SET title='并发正常' WHERE room_id=?", (str(room),))
    backend.after_apply = concurrent_write
    assert worker.run_once() == 1
    assert worker.status() == {"delivered": 1, "superseded": 1}
    actual = backend.points[(room, memory.memory_id)]
    assert actual.revision == 2 and actual.record.anchor.content_hash == hashlib.sha256(memory.content.encode()).hexdigest()
    assert worker.run_once() == 0


def test_missing_vector_waits_then_embedding_write_resumes(setup):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    assert worker.run_once() == 1
    assert worker.status() == {"waiting": 1}
    assert backend.points[(room, memory.memory_id)].record is None
    assert worker.run_once() == 0
    cache(store, memory)
    assert worker.run_once() == 1
    assert worker.status() == {"superseded": 1, "delivered": 1}
    assert backend.points[(room, memory.memory_id)].record is not None


@pytest.mark.parametrize("kind", ["hash", "model", "dimension", "nan", "zero", "blob"])
def test_invalid_or_stale_cache_never_delivered_as_active(setup, kind):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    cache(store, memory, model="other" if kind == "model" else "model-v1",
          vector=(1., 0., 0.) if kind == "dimension" else
          (float("nan"), 0.) if kind == "nan" else (0., 0.) if kind == "zero" else (1., 0.),
          content_hash="b" * 64 if kind == "hash" else None)
    if kind == "blob":
        with store.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_memory_embeddings SET vector=X'00' WHERE memory_id=?",
                         (str(memory.memory_id),))
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record is None
    assert worker.status()["waiting"] == 1


@pytest.mark.parametrize("action", ["retire", "replace", "complete", "cancel", "physical_delete"])
def test_lifecycle_tombstones_and_old_claim_cannot_resurrect(setup, action):
    store, service, room, backend, _, worker = setup
    if action in ("complete", "cancel"):
        source = service.post_message(room, content="@白金 下次要做压测", idempotency_key=uuid4(), reply_to=None).message
        memory = store.memory.capture_agent_proposal(room, source.message_id,
                                                    AgentMemoryProposal(kind="open_loop", content="下次要做压测"))
    else:
        memory = save(service, room)
    cache(store, memory)
    claim = worker.claim()
    old_mutation, _ = worker._materialize(claim)
    if action == "retire":
        store.memory.retire(room, memory.memory_id)
    elif action == "replace":
        save(service, room, "新事实", replaces_memory_id=memory.memory_id)
    elif action in ("complete", "cancel"):
        store.memory.close_open_loop(room, memory.memory_id, action=action)
    else:
        with store.database.transaction() as conn:
            conn.execute("DELETE FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(memory.memory_id),))
            conn.execute("DELETE FROM standalone_chat_memory_fts_documents WHERE memory_id=?", (str(memory.memory_id),))
            conn.execute("DELETE FROM standalone_chat_memories WHERE memory_id=?", (str(memory.memory_id),))
    worker.run_once()
    tombstone = backend.points[(room, memory.memory_id)]
    assert tombstone.record is None and tombstone.revision > old_mutation.revision
    # Delayed request already outside SQLite arrives AFTER newer tombstone.
    backend.apply_mutation(old_mutation)
    worker.deliver(claim)
    assert backend.points[(room, memory.memory_id)] == tombstone


def test_same_operation_retry_does_not_enqueue_again(setup):
    store, service, room, _, _, _ = setup
    memory = save(service, room)
    store.memory.create(room, memory.source_message_id, memory.content)
    assert len(rows(store)) == 1
    store.memory.retire(room, memory.memory_id)
    n = len(rows(store))
    store.memory.retire(room, memory.memory_id)
    assert len(rows(store)) == n


def test_failure_backoff_dead_letter_safe_error_and_manual_redrive(setup):
    store, service, room, backend, clock, worker = setup
    cache(store, save(service, room))
    worker.policy = SyncPolicy(max_attempts=2)
    backend.fail = True
    assert worker.run_once() == 1
    assert rows(store)[-1]["available_at"] == 102
    assert worker.run_once() == 0
    clock[0] = 102
    assert worker.run_once() == 1
    assert worker.status()["dead"] == 1
    assert "secret" not in json.dumps(rows(store))
    backend.fail = False
    assert worker.retry_dead() == 1
    assert worker.run_once() == 1
    assert worker.status()["delivered"] == 1


def test_restart_after_remote_success_before_local_ack_is_idempotent(setup):
    store, service, room, backend, clock, worker = setup
    memory = save(service, room)
    cache(store, memory)
    claim = worker.claim()
    mutation, _ = worker._materialize(claim)
    backend.apply_mutation(mutation)  # Process crashes before ACK.
    other = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend, clock=lambda: clock[0])
    assert other.run_once() == 0
    clock[0] += 61
    assert other.run_once() == 1
    assert len(backend.points) == 1 and other.status()["delivered"] == 1
    assert not worker._finish(claim, "delivered")  # Old token is fenced.


def test_expired_lease_takeover_is_atomic_and_exhausted_claim_dead(setup):
    store, service, room, backend, clock, worker = setup
    cache(store, save(service, room))
    worker.policy = SyncPolicy(max_attempts=2)
    first = worker.claim()
    other = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend, policy=worker.policy, clock=lambda: clock[0])
    assert other.claim() is None
    clock[0] += 61
    second = other.claim()
    assert second.event_id == first.event_id and second.token != first.token
    assert not worker._finish(first, "delivered")
    clock[0] += 61
    assert worker.claim() is None
    assert worker.status()["dead"] == 1


def test_concurrent_canonical_change_during_send_keeps_new_event(setup):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    cache(store, memory)
    backend.after_apply = lambda: store.memory.retire(room, memory.memory_id)
    worker.run_once(limit=1)
    assert rows(store)[1]["status"] == "superseded"
    assert rows(store)[-1]["status"] == "pending"
    backend.after_apply = None
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record is None


def test_canonical_hash_edit_and_embedding_delete_remove_old_remote(setup):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    cache(store, memory)
    worker.run_once()
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET content='改过的事实' WHERE memory_id=?", (str(memory.memory_id),))
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record is None
    updated = store.memory.list_active(room)[0]
    cache(store, updated)
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record.anchor.content_hash != hashlib.sha256(memory.content.encode()).hexdigest()
    with store.database.transaction() as conn:
        conn.execute("DELETE FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(memory.memory_id),))
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record is None


@pytest.mark.parametrize("kind", ["sensitive", "external", "external_root", "agent", "wrong_room"])
def test_source_privacy_changes_emit_fence_without_payload_text(setup, kind):
    store, service, room, backend, _, worker = setup
    if kind == "external_root":
        root = service.post_message(room, content="@白金 根消息", idempotency_key=uuid4(), reply_to=None).message
        source = service.post_message(room, content="@白金 原事实", idempotency_key=uuid4(), reply_to=None).message
        with store.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?",
                         (str(root.correlation_id), str(source.message_id)))
        memory = service.create_memory(room, source_message_id=source.message_id, content="原事实")
    else:
        memory = save(service, room, "密码仅用于本机" if kind == "sensitive" else "原事实")
    cache(store, memory)
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id if kind == "wrong_room" else None
    if kind != "sensitive":
        with store.database.transaction() as conn:
            source_id = root.message_id if kind == "external_root" else memory.source_message_id
            source = conn.execute("SELECT * FROM standalone_chat_messages WHERE message_id=?", (str(source_id),)).fetchone()
            message = json.loads(source["message_json"])
            if kind in ("external", "external_root"):
                message["external_source"] = {"provider": "test"}
                conn.execute("UPDATE standalone_chat_messages SET message_json=? WHERE message_id=?",
                             (json.dumps(message), str(source_id)))
            elif kind == "agent":
                sender = conn.execute("SELECT member_id FROM standalone_chat_members WHERE room_id=? AND role!='human' LIMIT 1", (str(room),)).fetchone()[0]
                conn.execute("UPDATE standalone_chat_messages SET sender_id=? WHERE message_id=?", (sender, str(memory.source_message_id)))
            else:
                conn.execute("UPDATE standalone_chat_messages SET room_id=?, correlation_id=? WHERE message_id=?", (str(other), str(uuid4()), str(memory.source_message_id)))
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record is None


def test_source_correlation_change_refreshes_anchor(setup):
    store, service, room, backend, _, worker = setup
    memory = save(service, room)
    cache(store, memory)
    correlation = uuid4()
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?", (str(correlation), str(memory.source_message_id)))
    worker.run_once()
    assert backend.points[(room, memory.memory_id)].record.anchor.source_correlation_id == correlation


def test_model_targets_receive_deletes_independently_and_default_no_worker(setup):
    store, service, room, backend, _, worker = setup
    other = VersionedFake()
    other.model, other.sync_target_id = VectorModel("model-v2", 2), "b" * 64
    second = MemoryVectorOutbox(store.database, other)
    second.register_target()
    memory = save(service, room)
    cache(store, memory)
    worker.run_once()
    second.run_once()
    cache(store, memory, model="model-v2")
    worker.run_once()
    second.run_once()
    assert backend.points[(room, memory.memory_id)].record is None
    assert other.points[(room, memory.memory_id)].record.model == other.model
    store.memory.retire(room, memory.memory_id)
    worker.run_once()
    second.run_once()
    assert all(b.points[(room, memory.memory_id)].record is None for b in (backend, other))


def test_register_after_existing_memories_has_no_implicit_backfill(setup):
    store, service, room, _, _, _ = setup
    save(service, room)
    other = VersionedFake()
    other.sync_target_id = "c" * 64
    worker = MemoryVectorOutbox(store.database, other)
    worker.register_target()
    worker.register_target()
    assert worker.status() == {} and worker.run_once() == 0
    assert other.calls == []
    other.model = VectorModel("changed", 2)
    with pytest.raises(ValueError):
        worker.register_target()


def test_identity_guard_and_failed_embedding_rollback(setup):
    store, service, room, _, _, _ = setup
    memory = save(service, room)
    cache(store, memory)
    for column in ("memory_id", "room_id"):
        with pytest.raises(sqlite3.IntegrityError), store.database.transaction() as conn:
            conn.execute(f"UPDATE standalone_chat_memories SET {column}=? WHERE memory_id=?", (str(uuid4()), str(memory.memory_id)))
    with pytest.raises(RuntimeError), store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memory_embeddings SET vector=X'00' WHERE memory_id=?", (str(memory.memory_id),))
        raise RuntimeError("rollback cache")
    assert len(rows(store)) == 2


@pytest.mark.parametrize("field,value", [("revision", 0), ("revision", True),
    ("revision", MAX_SYNC_REVISION + 1), ("room_id", "not-uuid"), ("model", "model")])
def test_mutation_validation(field, value):
    mutation = VectorMutation(uuid4(), uuid4(), 1, VectorModel("model", 2))
    with pytest.raises((ValueError, TypeError)):
        replace(mutation, **{field: value})


def test_mutation_mismatched_and_zero_records_rejected():
    room, memory = uuid4(), uuid4()
    model = VectorModel("model", 2)
    anchor = VectorAnchor(memory, room, uuid4(), uuid4(), "a" * 64)
    for record in (VectorRecord(anchor, model, (0., 0.)),
                   VectorRecord(replace(anchor, room_id=uuid4()), model, (1., 0.))):
        with pytest.raises(ValueError):
            VectorMutation(room, memory, 1, model, record)


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"max_attempts": True},
    {"lease_seconds": float("nan")}, {"retry_base_seconds": 0},
    {"retry_base_seconds": 10, "retry_cap_seconds": 5}])
def test_policy_validation(kwargs):
    with pytest.raises(ValueError):
        SyncPolicy(**kwargs)


def test_operator_plan_does_not_load_settings_or_database(monkeypatch, capsys):
    from app.chat import vector_sync
    monkeypatch.setattr(vector_sync, "Settings", lambda: pytest.fail("plan read private config"))
    vector_sync.main([])
    assert "Plan only" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        vector_sync.main(["--initialize-target"])


def test_two_connections_cannot_claim_the_same_event(setup):
    store, service, room, backend, _, worker = setup
    cache(store, save(service, room))
    other = MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend, clock=lambda: 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda w: w.claim(), (worker, other)))
    assert sum(claim is not None for claim in claims) == 1


def test_migration_of_old_database_keeps_facts_and_does_not_backfill(tmp_path):
    from app.chat.memory import CHAT_MEMORY_MIGRATIONS
    database = SQLiteDatabase(tmp_path / "old.sqlite3")
    database.initialize((*STANDALONE_CHAT_MIGRATIONS, *[m for m in CHAT_MEMORY_MIGRATIONS if m.version < 27]))
    store = StandaloneChatStore(database)
    store.memory.fts_index.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="legacy", idempotency_key=uuid4()).room_id
    memory = save(service, room)
    cache(store, memory)
    store.initialize()
    store.initialize()
    worker = MemoryVectorOutbox(database, VersionedFake())
    worker.register_target()
    assert database.schema_version == 29 and store.memory.list_active(room) == (memory,)
    assert worker.status() == {}
    with database.connect() as conn:
        assert conn.execute("SELECT revision FROM standalone_chat_vector_revisions").fetchone()[0] == 1
    store.memory.retire(room, memory.memory_id)
    assert worker.run_once() == 1


def test_unregistered_or_conflicting_target_cannot_claim_or_redrive(setup):
    store, service, room, backend, _, _ = setup
    cache(store, save(service, room))
    backend.sync_target_id = "f" * 64
    worker = MemoryVectorOutbox(store.database, backend)
    with pytest.raises(ValueError):
        worker.claim()
    with pytest.raises(ValueError):
        worker.retry_dead()


@pytest.mark.parametrize("value", [0, 201, True])
def test_operator_batch_limits(setup, value):
    worker = setup[-1]
    with pytest.raises(ValueError):
        worker.run_once(limit=value)
    with pytest.raises(ValueError):
        worker.retry_dead(limit=value)


def test_operator_status_has_no_network_or_fact_mutations(setup, monkeypatch, capsys):
    from app.chat import vector_sync
    from app.config import Settings
    store, service, room, _, _, _ = setup
    memory = save(service, room)
    before = rows(store)
    monkeypatch.setattr(vector_sync, "Settings", lambda: Settings(_env_file=None))
    vector_sync.main(["--status", "--database", str(store.database.path),
                      "--model-fingerprint", "model-v1", "--dimensions", "2"])
    assert '"attempted": 0' in capsys.readouterr().out
    assert rows(store) == before and store.memory.list_active(room) == (memory,)


def test_probe_plan_and_connection_failure_do_not_create_or_delete_collections(tmp_path, monkeypatch, capsys):
    from app.config import Settings
    from evals import check_memory_vector_sync as probe_module
    monkeypatch.setattr(probe_module, "Settings", lambda: pytest.fail("plan loaded config"))
    monkeypatch.setattr("sys.argv", ["probe"])
    probe_module.main()
    assert "Plan only" in capsys.readouterr().out
    monkeypatch.setattr(probe_module, "Settings", lambda: Settings(_env_file=None))
    calls = []
    def failed(self, method, path, *args, **kwargs):
        calls.append(method)
        raise OSError("private-body")
    monkeypatch.setattr(probe_module.QdrantVersionedVectorBackend, "_request", failed)
    output, report = probe_module.probe(output_root=tmp_path)
    assert report["complete"] is False and calls == ["GET"]
    assert "private-body" not in (output / "report.json").read_text()


def test_probe_never_adopts_or_deletes_existing_collection(tmp_path, monkeypatch):
    from app.config import Settings
    from evals import check_memory_vector_sync as probe_module
    monkeypatch.setattr(probe_module, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(probe_module.QdrantVersionedVectorBackend, "_collection", lambda self: {})
    monkeypatch.setattr(probe_module.QdrantVersionedVectorBackend, "_request",
                        lambda *args, **kwargs: pytest.fail("collision must not initialize/delete"))
    _, report = probe_module.probe(output_root=tmp_path)
    assert not report["complete"] and not report["cleanup_complete"]
