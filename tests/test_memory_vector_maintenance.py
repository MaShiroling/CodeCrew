"""Maintenance checkpoints/safety using synthetic SQLite and fake storage."""

import hashlib
import urllib.request
from dataclasses import replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord
from app.chat.vector_maintenance import MemoryVectorMaintenance, VectorMaintenanceError
from app.chat.vector_maintenance_contracts import (
    RemoteIdentity,
    RemotePage,
    VectorInspection,
    cursor_key,
)
from app.chat.vector_outbox import MemoryVectorOutbox
from app.storage import SQLiteDatabase


class MaintainableFake:
    model = VectorModel("model-v1", 2)
    sync_target_id = "a" * 64

    def __init__(self):
        self.points, self.extra = {}, []
        self.calls = []
        self.fail = False
        self.inspect_hook = None
        self.inspection_code = None

    @staticmethod
    def point_id(mutation):
        return str(uuid5(NAMESPACE_URL, f"codecrew:vector:v1:{mutation.room_id}:{mutation.memory_id}"))

    def apply_mutation(self, mutation):
        key = self.point_id(mutation)
        existing = self.points.get(key)
        if existing is None or existing.revision < mutation.revision:
            self.points[key] = mutation
        elif existing.revision == mutation.revision and existing != mutation:
            raise RuntimeError("revision conflict")
        return self.points[key].revision

    def inspect_mutations(self, mutations):
        self.calls.append(("inspect", len(mutations)))
        if self.fail:
            raise RuntimeError("private-provider-body")
        if self.inspect_hook:
            hook, self.inspect_hook = self.inspect_hook, None
            hook()
        result = []
        for mutation in mutations:
            existing = self.points.get(self.point_id(mutation))
            code = self.inspection_code or ("missing" if existing is None else
                "ahead" if existing.revision > mutation.revision else "current" if existing == mutation else "drift")
            result.append(VectorInspection(mutation.memory_id, code, existing.revision if existing else None))
        return tuple(result)

    def scan_identities(self, cursor=None, *, limit=16):
        self.calls.append(("scroll", limit, cursor))
        if self.fail:
            raise RuntimeError("private-provider-body")
        identities = sorted([RemoteIdentity(key, item.room_id, item.memory_id) for key, item in self.points.items()]
                            + self.extra, key=lambda item: cursor_key(item.point_id))
        selected = [item for item in identities if cursor is None or cursor_key(item.point_id) >= cursor_key(cursor)]
        return RemotePage(tuple(selected[:limit]), selected[limit].point_id if len(selected) > limit else None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("offline test called provider"))
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "maintenance.sqlite3"))
    store.initialize()
    service = StandaloneChatService(store)
    room = service.create_room(title="maintenance", idempotency_key=uuid4()).room_id
    backend = MaintainableFake()
    worker = MemoryVectorOutbox(store.database, backend)
    return store, service, room, backend, worker, MemoryVectorMaintenance(worker)


def save(service, room, text):
    source = service.post_message(room, content="@白金 " + text, idempotency_key=uuid4(), reply_to=None).message
    return service.create_memory(room, source_message_id=source.message_id, content=text)


def cache(store, memory, model=None):
    with store.database.connect() as conn:
        source = conn.execute("SELECT correlation_id FROM standalone_chat_messages WHERE message_id=?",
                              (str(memory.source_message_id),)).fetchone()
    anchor = VectorAnchor(memory.memory_id, memory.room_id, memory.source_message_id, UUID(source[0]),
                          hashlib.sha256(memory.content.encode()).hexdigest())
    SQLiteMemoryVectorBackend(store.database).upsert((VectorRecord(anchor, model or VectorModel("model-v1", 2), (1., 0.)),))


def history(setup, n=3):
    store, service, room, _, worker, _ = setup
    memories = [save(service, room, f"历史事实{i}") for i in range(n)]
    for memory in memories:
        cache(store, memory)
    assert worker.status() == {}  # Cache was written before subscription.
    worker.register_target()
    return memories


def finish(maintenance, run, limit=2):
    for _ in range(30):
        status = maintenance.step(run, limit=limit)
        if status["scan_complete"]:
            return status
    pytest.fail("bounded synthetic run did not finish")


def seeded(setup):
    memories = history(setup)
    worker, maintenance = setup[-2:]
    run = maintenance.start()
    finish(maintenance, run)
    worker.run_once()
    return memories


def test_backfill_bounded_resume_and_repeat_are_idempotent(setup):
    store, _, _, backend, worker, maintenance = setup
    history(setup, 5)
    run = maintenance.start()
    first = maintenance.step(run, limit=2)
    assert first["counts"] == {"local_scanned": 2, "enqueued": 2}
    assert not first["scan_complete"] and backend.calls == []
    restarted = MemoryVectorMaintenance(MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend))
    final = finish(restarted, run, limit=2)
    assert final["counts"]["local_scanned"] == final["counts"]["enqueued"] == 5
    assert not final["consistent_at_observation"]  # Enqueued is not audited/delivered.
    assert worker.run_once() == 5 and len(backend.points) == 5
    assert restarted.step(run) == restarted.status(run)
    again = finish(restarted, restarted.start())
    assert again["counts"]["enqueued"] == 0 and worker.run_once() == 0


def test_enqueue_failure_rolls_back_cursor_and_entire_page(setup):
    store, _, _, _, worker, maintenance = setup
    history(setup)
    run = maintenance.start()
    with store.database.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_backfill BEFORE INSERT ON standalone_chat_vector_outbox "
                     "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(VectorMaintenanceError):
        maintenance.step(run)
    assert maintenance.status(run)["checkpoint"] == 0 and worker.status() == {}
    assert maintenance.status(run)["error_code"] == "maintenance_failed"
    with store.database.transaction() as conn:
        conn.execute("DROP TRIGGER fail_backfill")
    assert finish(maintenance, run)["counts"]["enqueued"] == 3


def test_two_way_audit_pagination_reports_consistency_without_mutating_facts(setup):
    store, _, room, backend, worker, maintenance = setup
    seeded(setup)
    before = store.memory.list_active(room)
    status = finish(maintenance, maintenance.start(mode="audit"), limit=1)
    assert status["consistent_at_observation"]
    assert status["counts"] == {"local_scanned": 3, "current": 3, "remote_scanned": 3}
    assert store.memory.list_active(room) == before and worker.run_once() == 0
    assert any(call[0] == "scroll" and call[2] is not None for call in backend.calls)


def test_read_failure_preserves_checkpoint_and_resume_clears_safe_error(setup):
    _, _, _, backend, _, maintenance = setup
    seeded(setup)
    run = maintenance.start(mode="audit")
    backend.fail = True
    with pytest.raises(VectorMaintenanceError):
        maintenance.step(run)
    status = maintenance.status(run)
    assert status["checkpoint"] == 0 and status["counts"] == {}
    assert "private" not in str(status)
    backend.fail = False
    assert finish(maintenance, run)["consistent_at_observation"]


def test_remote_scroll_failure_preserves_remote_cursor(setup):
    _, _, _, backend, _, maintenance = setup
    seeded(setup)
    run = maintenance.start(mode="audit")
    maintenance.step(run)
    before = maintenance.status(run)
    assert before["phase"] == "remote"
    backend.fail = True
    with pytest.raises(VectorMaintenanceError):
        maintenance.step(run)
    assert maintenance.status(run)["checkpoint"] == before["checkpoint"]
    backend.fail = False
    assert finish(maintenance, run)["consistent_at_observation"]


@pytest.mark.parametrize("kind", ["missing", "content", "vector", "retired"])
def test_repair_enqueues_new_revision_and_blocks_old_delayed_write(setup, kind):
    store, _, room, backend, worker, maintenance = setup
    seeded(setup)
    key, original = next(iter(backend.points.items()))
    if kind == "missing":
        del backend.points[key]
    elif kind == "retired":
        store.memory.retire(room, original.memory_id)
        worker.run_once()
        backend.points[key] = original  # Simulate external overwrite bypassing fences.
    elif kind == "content":
        backend.points[key] = replace(original, record=replace(original.record,
            anchor=replace(original.record.anchor, content_hash="b" * 64)))
    else:
        backend.points[key] = replace(original, record=replace(original.record, vector=(0., 1.)))
    before_facts = store.memory.list_active(room)
    run = maintenance.start(mode="repair")
    final = finish(maintenance, run)
    assert final["counts"]["repair_enqueued"] == 1
    assert not final["consistent_at_observation"]  # Requires delivery and a fresh audit.
    with store.database.connect() as conn:
        assert conn.execute("SELECT revision FROM standalone_chat_vector_revisions WHERE memory_id=?",
                            (str(original.memory_id),)).fetchone()[0] > original.revision
    worker.run_once()
    expected = backend.points[key]
    assert expected.record is None if kind == "retired" else expected.record.anchor == original.record.anchor
    backend.apply_mutation(original)
    assert backend.points[key] == expected
    assert store.memory.list_active(room) == before_facts
    assert finish(maintenance, maintenance.start(mode="audit"))["consistent_at_observation"]


@pytest.mark.parametrize("code", ["ahead", "scope_conflict", "invalid_revision"])
def test_ambiguous_or_higher_remote_state_is_never_auto_overwritten(setup, code):
    store, _, _, backend, worker, maintenance = setup
    seeded(setup)
    backend.inspection_code = code
    original = dict(backend.points)
    with store.database.connect() as conn:
        revisions = list(conn.execute("SELECT revision FROM standalone_chat_vector_revisions"))
    run = maintenance.start(mode="repair")
    final = finish(maintenance, run)
    assert final["counts"][code] == 3 and final["counts"].get("repair_enqueued", 0) == 0
    assert maintenance.findings(run)[0]["code"] == code
    assert worker.run_once() == 0 and backend.points == original
    with store.database.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT revision FROM standalone_chat_vector_revisions")] == [row[0] for row in revisions]


def test_canonical_change_during_remote_read_is_revalidated_before_repair(setup):
    store, _, room, backend, worker, maintenance = setup
    memories = seeded(setup)
    missing = next(iter(backend.points))
    del backend.points[missing]
    first = min(memories, key=lambda memory: str(memory.memory_id))
    backend.inspect_hook = lambda: store.memory.retire(room, first.memory_id)
    status = finish(maintenance, maintenance.start(mode="repair"))
    assert status["counts"]["changed_during_page"] == 1
    assert status["changed_since_start"]
    worker.run_once()


def test_concurrent_resume_only_one_commits_page_repairs(setup):
    store, _, _, backend, worker, maintenance = setup
    seeded(setup)
    backend.points.clear()
    run = maintenance.start(mode="repair")
    second = MemoryVectorMaintenance(MemoryVectorOutbox(SQLiteDatabase(store.database.path), backend))
    backend.inspect_hook = lambda: second.step(run, limit=16)
    status = maintenance.step(run, limit=16)
    assert status["counts"]["repair_enqueued"] == 3
    with store.database.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT revision FROM standalone_chat_vector_revisions")} == {3}
    assert worker.run_once() == 3


def test_orphans_and_unmanaged_points_are_reported_not_adopted_or_deleted(setup):
    _, _, _, backend, worker, maintenance = setup
    seeded(setup)
    original = next(iter(backend.points.values()))
    orphan = replace(original, memory_id=uuid4(), record=None)
    backend.points[backend.point_id(orphan)] = orphan
    backend.extra = [RemoteIdentity(0), RemoteIdentity(str(uuid4()))]
    before = dict(backend.points)
    run = maintenance.start(mode="repair")
    status = finish(maintenance, run, limit=1)
    assert status["counts"]["orphan"] == 1 and status["counts"]["unmanaged"] == 2
    assert backend.points == before and worker.run_once() == 0
    assert any(item["code"] == "orphan" for item in maintenance.findings(run))
    assert not status["consistent_at_observation"]


def test_waiting_cache_is_not_a_successful_full_rebuild(setup):
    store, service, room, _, worker, maintenance = setup
    save(service, room, "尚无向量")
    worker.register_target()
    backfill = finish(maintenance, maintenance.start())
    assert backfill["counts"]["cache_waiting"] == 1
    worker.run_once()
    audit = finish(maintenance, maintenance.start(mode="audit"))
    assert audit["counts"]["current"] == 1 and audit["counts"]["cache_waiting"] == 1
    assert audit["outbox"]["waiting"] == 1 and not audit["consistent_at_observation"]
    assert store.memory.list_active(room)[0].content == "尚无向量"


def test_changed_epoch_blocks_clean_report_even_if_worker_already_delivered(setup):
    store, _, _, _, worker, maintenance = setup
    memories = seeded(setup)
    run = maintenance.start(mode="audit")
    maintenance.step(run)
    cache(store, memories[0])
    worker.run_once()
    final = finish(maintenance, run)
    assert final["changed_since_start"] and not final["consistent_at_observation"]
    assert finish(maintenance, maintenance.start(mode="audit"))["consistent_at_observation"]


def test_high_water_future_insert_before_cursor_is_captured_by_subscription(setup):
    store, _, _, backend, worker, maintenance = setup
    memories = history(setup)
    run = maintenance.start()
    maintenance.step(run, limit=1)
    low_id = str(UUID(int=1))
    with store.database.transaction() as conn:
        conn.execute("INSERT INTO standalone_chat_memories(memory_id,room_id,source_message_id,content,status,created_at) "
                     "VALUES (?,?,?,'新插入但ID更小','active','today')",
                     (low_id, str(memories[0].room_id), str(memories[0].source_message_id)))
        assert conn.execute("SELECT count(*) FROM standalone_chat_vector_outbox WHERE memory_id=?", (low_id,)).fetchone()[0] == 1
    status = finish(maintenance, run)
    assert status["counts"]["local_scanned"] == 3
    assert worker.run_once() == 4 and len(backend.points) == 4


def test_model_rebuild_targets_remain_separate_and_reuse_only_matching_cache(setup):
    store, _, _, backend, worker, _ = setup
    seeded(setup)
    original = dict(backend.points)
    fresh = MaintainableFake()
    fresh.sync_target_id = "b" * 64
    fresh.model = VectorModel("model-v2", 2)
    second_worker = MemoryVectorOutbox(store.database, fresh)
    second_worker.register_target()
    second = MemoryVectorMaintenance(second_worker)
    status = finish(second, second.start())
    assert status["counts"]["cache_waiting"] == 3
    second_worker.run_once()
    assert all(item.record is None for item in fresh.points.values()) and backend.points == original
    assert worker.run_once() == 0


def test_empty_history_and_read_only_status(setup):
    _, _, _, backend, worker, maintenance = setup
    worker.register_target()
    run = maintenance.start(mode="audit")
    final = finish(maintenance, run)
    calls = list(backend.calls)
    assert final["consistent_at_observation"] and final["counts"] == {}
    assert maintenance.status(run) == final and backend.calls == calls


def test_cross_target_resume_and_invalid_mode_rejected(setup):
    store, _, _, _, _, maintenance = setup
    history(setup)
    run = maintenance.start()
    other = MaintainableFake()
    other.sync_target_id = "b" * 64
    worker = MemoryVectorOutbox(store.database, other)
    worker.register_target()
    with pytest.raises(ValueError):
        MemoryVectorMaintenance(worker).status(run)
    with pytest.raises(ValueError):
        maintenance.start(mode="drop")


@pytest.mark.parametrize("limit", [0, 17, True])
def test_page_limits(setup, limit):
    history(setup)
    maintenance = setup[-1]
    run = maintenance.start()
    with pytest.raises(ValueError):
        maintenance.step(run, limit=limit)
    with pytest.raises(ValueError):
        maintenance.findings(run, limit=limit)


@pytest.mark.parametrize("bad", [True, -1, 18_446_744_073_709_551_616, "private-text"])
def test_cursor_validation(bad):
    with pytest.raises(ValueError):
        cursor_key(bad)


def test_page_cursor_must_advance_and_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        RemotePage((RemoteIdentity(1),), 1)
    with pytest.raises(ValueError):
        RemotePage((RemoteIdentity(1), RemoteIdentity(1)), None)


def test_operator_plan_and_flag_guards(monkeypatch, capsys):
    from app.chat import vector_maintain
    monkeypatch.setattr(vector_maintain, "Settings", lambda: pytest.fail("plan read config"))
    vector_maintain.main([])
    assert "Plan only" in capsys.readouterr().out
    for args in (["--new-target"], ["--deliver"], ["--run", "--new-target"],
                 ["--run", "--new-target", "--collection-prefix", "fresh", "--mode", "repair"]):
        with pytest.raises(SystemExit):
            vector_maintain.main(args)


def test_repair_transaction_rollback_preserves_revision_and_checkpoint(setup):
    store, _, _, backend, _, maintenance = setup
    seeded(setup)
    backend.points.clear()
    run = maintenance.start(mode="repair")
    with store.database.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON standalone_chat_vector_outbox "
                     "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(VectorMaintenanceError):
        maintenance.step(run)
    with store.database.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT revision FROM standalone_chat_vector_revisions")} == {2}
    assert maintenance.status(run)["checkpoint"] == 0


def test_backfill_includes_retired_and_physically_deleted_identity_tombstones(setup):
    store, _, room, backend, worker, maintenance = setup
    memories = history(setup)
    store.memory.retire(room, memories[0].memory_id)
    with store.database.transaction() as conn:
        conn.execute("DELETE FROM standalone_chat_memory_fts_documents WHERE memory_id=?", (str(memories[1].memory_id),))
        conn.execute("DELETE FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(memories[1].memory_id),))
        conn.execute("DELETE FROM standalone_chat_memory_candidates WHERE memory_id=?", (str(memories[1].memory_id),))
        conn.execute("DELETE FROM standalone_chat_memories WHERE memory_id=?", (str(memories[1].memory_id),))
    finish(maintenance, maintenance.start())
    worker.run_once()
    assert sum(item.record is None for item in backend.points.values()) == 2
    assert finish(maintenance, maintenance.start(mode="audit"))["consistent_at_observation"]


def operator(setup, monkeypatch):
    from app.chat import vector_maintain
    store, _, _, backend, _, _ = setup
    settings = SimpleNamespace(memory_qdrant_url="http://127.0.0.1:6333", memory_qdrant_collection_prefix="fake",
        memory_qdrant_timeout_seconds=5, memory_qdrant_api_key=SimpleNamespace(get_secret_value=lambda: ""))
    monkeypatch.setattr(vector_maintain, "Settings", lambda: settings)
    monkeypatch.setattr(vector_maintain, "QdrantVersionedVectorBackend", lambda *a, **k: backend)
    backend._collection = lambda: pytest.fail("unexpected remote inventory")
    backend.initialize = lambda: pytest.fail("unexpected remote initialization")
    return vector_maintain, ["--database", str(store.database.path), "--model-fingerprint", "model-v1", "--dimensions", "2"]


def test_operator_status_is_read_only_and_mode_guarded(setup, monkeypatch, capsys):
    history(setup)
    maintenance = setup[-1]
    run = maintenance.start()
    module, args = operator(setup, monkeypatch)
    module.main(["--status", "--run-id", str(run), *args])
    assert '"checkpoint": 0' in capsys.readouterr().out and setup[3].calls == []
    with pytest.raises(SystemExit):
        module.main(["--run", "--run-id", str(run), "--mode", "repair", *args])
    assert maintenance.status(run)["checkpoint"] == 0


def test_operator_backfill_resume_and_explicit_delivery(setup, monkeypatch, capsys):
    history(setup)
    module, args = operator(setup, monkeypatch)
    run = setup[-1].start()
    module.main(["--run", "--run-id", str(run), "--limit", "1", *args])
    assert '"checkpoint": 1' in capsys.readouterr().out and not setup[3].points
    module.main(["--run", "--run-id", str(run), "--deliver", *args])
    assert '"delivery_attempted": 3' in capsys.readouterr().out and len(setup[3].points) == 3


@pytest.mark.parametrize("collision", ["local", "remote"])
def test_new_target_collision_never_adopts_initializes_or_deletes(setup, monkeypatch, collision):
    module, args = operator(setup, monkeypatch)
    backend, worker = setup[3:5]
    if collision == "local":
        worker.register_target()
    else:
        backend._collection = dict
    with pytest.raises(SystemExit):
        module.main(["--run", "--new-target", "--collection-prefix", "fresh", *args])
    assert backend.calls == []


def test_new_target_checks_schema_before_remote_creation(setup, monkeypatch):
    module, args = operator(setup, monkeypatch)
    with setup[0].database.transaction() as conn:
        conn.execute("DROP TABLE standalone_chat_vector_maintenance_runs")
    with pytest.raises(SystemExit):
        module.main(["--run", "--new-target", "--collection-prefix", "fresh", *args])
    assert setup[3].calls == []


def test_live_probe_default_does_not_open_config_or_network(monkeypatch, capsys):
    from evals import check_memory_vector_maintenance as probe
    monkeypatch.setattr(probe, "Settings", lambda: pytest.fail("plan read private config"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.parametrize("collision", [True, False])
def test_live_probe_failure_keeps_safe_report_and_never_deletes_collision(tmp_path, monkeypatch, collision):
    from evals import check_memory_vector_maintenance as probe
    instances = []
    class Backend:
        def __init__(self, endpoint, model):
            self.collection_name = endpoint.collection_prefix
            self._path = "/collections/" + self.collection_name
            self.exists = collision
            self.deleted = False
            instances.append(self)
        def _collection(self):
            return {} if self.exists else None
        def initialize(self):
            self.exists = True
            raise OSError("private-provider-body")
        def _request(self, method, path):
            assert method == "DELETE" and path == self._path
            self.deleted, self.exists = True, False
            return True
    monkeypatch.setattr(probe, "QdrantVersionedVectorBackend", Backend)
    output, report = probe.probe(output_root=tmp_path)
    assert not report["complete"] and not report["cleanup_complete"]
    assert instances[0].deleted is not collision and not instances[1].deleted
    assert "private-provider" not in (output / "report.json").read_text()
    assert report["embedding_calls"] == report["agent_calls"] == 0
