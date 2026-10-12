"""Actual MATCH and fault-injection checks; all databases are temporary."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.chat.fts_memory import MemoryFTSIndex
from app.chat.fts_text import FTSTextLimitError, build_fts_query, encode_fts_document
from app.chat.memory import CHAT_MEMORY_MIGRATIONS, AgentMemoryAction, AgentMemoryProposal
from app.chat.service import StandaloneChatService
from app.chat.store import STANDALONE_CHAT_MIGRATIONS, StandaloneChatStore
from app.storage import SQLiteDatabase


@pytest.fixture
def setup(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    if store.memory.fts_index.status()["status"] == "unavailable":
        pytest.skip("runtime has no FTS5; degradation tested separately")
    assert store.memory.fts_index.status()["usable"]
    service = StandaloneChatService(store)
    room = service.create_room(title="FTS test", idempotency_key=uuid4()).room_id
    return store, service, room


def source(service, room, text):
    return service.post_message(
        room, content="@白金 " + text, idempotency_key=uuid4(), reply_to=None,
    ).message


def save(service, room, text, **kwargs):
    message = source(service, room, text)
    return service.create_memory(
        room, source_message_id=message.message_id, content=text, **kwargs,
    )


def docs(store):
    with store.database.connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM standalone_chat_memory_fts_documents ORDER BY memory_id",
        )]


def matches(store, query):
    with store.database.connect() as conn:
        return {row[0] for row in conn.execute(
            "SELECT doc.memory_id FROM standalone_chat_memory_fts "
            "JOIN standalone_chat_memory_fts_documents doc "
            "ON doc.rowid = standalone_chat_memory_fts.rowid "
            "WHERE standalone_chat_memory_fts MATCH ?", (build_fts_query(query),),
        )}


def canonical(store):
    with store.database.connect() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("standalone_chat_memories", "standalone_chat_messages")}


def restart(store):
    store.initialize()
    assert store.memory.fts_index.status()["usable"]
    with store.database.transaction() as conn:
        store.memory.fts_index._integrity_check(conn)


def test_empty_index_and_manual_projection(setup):
    store, service, room = setup
    assert docs(store) == []
    memory = save(service, room, "SQLite 读超时 8 秒")
    row, = docs(store)
    message = store.get_message(memory.source_message_id).message
    assert row["memory_id"] == str(memory.memory_id)
    assert row["room_id"] == str(room)
    assert row["source_correlation_id"] == str(message.correlation_id)
    assert row["content_hash"] == hashlib.sha256(memory.content.encode()).hexdigest()
    assert row["revision"] == store.memory.fts_index.revision
    assert row["tokens"] == encode_fts_document(memory.content)
    assert matches(store, "SQLite") == {str(memory.memory_id)}
    assert "tokens" not in store.memory.fts_index.status()


def test_idempotent_manual_write_restart_and_concurrent_replay(setup):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    before = docs(store)

    def repeat(_):
        return service.create_memory(room, source_message_id=memory.source_message_id,
                                     content=memory.content)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(item == memory for item in pool.map(repeat, range(8)))
    restart(store)
    assert docs(store) == before
    assert matches(store, "标题") == {str(memory.memory_id)}


def test_auto_proposal_and_replay_indexed_once(setup):
    store, service, room = setup
    message = source(service, room, "项目决定使用 PostgreSQL")
    proposal = AgentMemoryProposal(kind="decision", content="项目决定使用 PostgreSQL")
    memory = store.memory.capture_agent_proposal(room, message.message_id, proposal)
    assert memory is not None
    before = docs(store)
    assert store.memory.capture_agent_proposal(room, message.message_id, proposal) == memory
    assert docs(store) == before
    assert matches(store, "PostgreSQL") == {str(memory.memory_id)}


def test_pending_candidate_not_indexed_until_approved(setup):
    store, service, room = setup
    message = source(service, room, "我们决定项目使用 SQLite")
    with store.database.transaction() as conn:
        candidate = store.memory.observe_message(conn, room, message.message_id, message.content)
    assert candidate is not None
    assert docs(store) == []
    _, memory = store.memory.approve_candidate(room, candidate.candidate_id,
                                               "项目使用 SQLite")
    before = docs(store)
    store.memory.approve_candidate(room, candidate.candidate_id, "项目使用 SQLite")
    assert docs(store) == before
    assert matches(store, "SQLite") == {str(memory.memory_id)}


def test_rejected_candidate_never_enters_index(setup):
    store, service, room = setup
    message = source(service, room, "更正：默认使用 SQLite，不是内存存储")
    with store.database.transaction() as conn:
        candidate = store.memory.observe_message(conn, room, message.message_id, message.content)
    assert candidate is not None
    store.memory.reject_candidate(room, candidate.candidate_id)
    with pytest.raises(ValueError, match="rejected"):
        store.memory.approve_candidate(room, candidate.candidate_id, "默认使用 SQLite")
    restart(store)
    assert docs(store) == []
    assert matches(store, "SQLite") == set()


def test_room_scope_and_illegal_replacement_leave_index_unchanged(setup):
    store, service, room = setup
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id
    old = save(service, room, "数据库使用 SQLite")
    neighbor = save(service, other, "数据库使用 PostgreSQL")
    projection = docs(store)
    message = source(service, other, "数据库改用 MySQL")
    with pytest.raises(ValueError, match="in this room"):
        store.memory.create(other, message.message_id, "数据库改用 MySQL",
                            replaces_memory_id=old.memory_id)
    assert docs(store) == projection
    assert {row["memory_id"]: row["room_id"] for row in projection} == {
        str(old.memory_id): str(room), str(neighbor.memory_id): str(other),
    }
    assert store.memory.retire(other, old.memory_id) is None
    assert docs(store) == projection


def test_projection_update_trigger_removes_old_terms(setup):
    store, service, room = setup
    memory = save(service, room, "数据库使用 SQLite")
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memory_fts_documents SET tokens=? WHERE memory_id=?",
                     (encode_fts_document("PostgreSQL"), str(memory.memory_id)))
    assert matches(store, "SQLite") == set()
    assert matches(store, "PostgreSQL") == {str(memory.memory_id)}
    # Direct projection modification is not an application write; startup audits
    # it against canonical content and repairs it, without accepting the change.
    restart(store)
    assert matches(store, "SQLite") == {str(memory.memory_id)}
    assert matches(store, "PostgreSQL") == set()


def test_replacement_removes_old_tokens_but_preserves_history(setup):
    store, service, room = setup
    old = save(service, room, "数据库使用 SQLite")
    new = save(service, room, "数据库改用 PostgreSQL", replaces_memory_id=old.memory_id)
    assert matches(store, "SQLite") == set()
    assert matches(store, "PostgreSQL") == {str(new.memory_id)}
    snapshot = canonical(store)
    restart(store)
    assert canonical(store) == snapshot
    assert new.replaces_memory_id == old.memory_id
    assert len(snapshot["standalone_chat_memories"]) == 2


def test_retire_and_repeat_do_not_resurrect(setup):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    store.memory.retire(room, memory.memory_id)
    store.memory.retire(room, memory.memory_id)
    snapshot = canonical(store)
    restart(store)
    assert matches(store, "标题") == set()
    assert docs(store) == []
    assert canonical(store) == snapshot
    assert len(snapshot["standalone_chat_memories"]) == 1


@pytest.mark.parametrize("action,state", [("complete", "completed"), ("cancel", "cancelled")])
@pytest.mark.parametrize("automatic", [False, True])
def test_close_open_loop_with_evidence_and_restart(setup, action, state, automatic):
    store, service, room = setup
    message = source(service, room, "下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(room, message.message_id,
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
    assert memory is not None
    assert matches(store, "文档链接") == {str(memory.memory_id)}
    evidence = source(service, room, "文档链接检查已经完成" if action == "complete"
                      else "取消文档链接检查")
    if automatic:
        proposal = AgentMemoryAction(action=action, memory_id=memory.memory_id,
                                     evidence=evidence.content.replace("@白金 ", ""))
        closed = store.memory.capture_agent_action(room, evidence.message_id, proposal)
        assert closed is not None
        assert closed.closed_by_message_id == evidence.message_id
    else:
        closed = store.memory.close_open_loop(room, memory.memory_id, action=action)
    assert closed.open_loop_state == state
    assert store.memory.close_open_loop(room, memory.memory_id, action=action) == closed
    snapshot = canonical(store)
    restart(store)
    assert canonical(store) == snapshot
    assert docs(store) == []
    assert matches(store, "文档链接") == set()


@pytest.mark.parametrize("replacement", [False, True])
def test_outer_transaction_rollback_restores_canonical_and_fts(setup, replacement):
    store, service, room = setup
    old = save(service, room, "数据库使用 SQLite")
    message = source(service, room, "数据库改用 PostgreSQL")
    before, projection = canonical(store), docs(store)
    with pytest.raises(RuntimeError, match="outer failure"), store.database.transaction() as conn:
        store.memory._create(conn, room, message.message_id, "数据库改用 PostgreSQL",
                             kind="fact", replaces_memory_id=old.memory_id if replacement else None)
        raise RuntimeError("outer failure")
    assert canonical(store) == before
    assert docs(store) == projection
    assert matches(store, "SQLite") == {str(old.memory_id)}
    assert matches(store, "PostgreSQL") == set()


def test_partial_replacement_index_failure_rolls_back_derivative_and_marks_dirty(setup, monkeypatch):
    store, service, room = setup
    old = save(service, room, "数据库使用 SQLite")
    projection = docs(store)
    real_sync = store.memory.fts_index._sync_one

    def fail_second(conn, memory_id):
        real_sync(conn, memory_id)
        if memory_id == old.memory_id:
            raise sqlite3.OperationalError("simulated index failure")

    with monkeypatch.context() as patch:
        patch.setattr(store.memory.fts_index, "_sync_one", fail_second)
        new = save(service, room, "数据库改用 PostgreSQL", replaces_memory_id=old.memory_id)
    assert docs(store) == projection  # stale, but explicitly untrusted
    status = store.memory.fts_index.status()
    assert status["status"] == "dirty" and not status["usable"]
    assert store.memory.retrieve(room, "PostgreSQL") == (new,)
    restart(store)
    assert matches(store, "SQLite") == set()
    assert matches(store, "PostgreSQL") == {str(new.memory_id)}


@pytest.mark.parametrize("operation", ["retire", "close"])
def test_failed_delete_sync_commits_retirement_with_dirty_marker(setup, monkeypatch, operation):
    store, service, room = setup
    message = source(service, room, "下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(room, message.message_id,
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
    real_sync = store.memory.fts_index._sync_one

    def fail(conn, memory_id):
        real_sync(conn, memory_id)
        raise sqlite3.DatabaseError("simulated deletion failure")

    with monkeypatch.context() as patch:
        patch.setattr(store.memory.fts_index, "_sync_one", fail)
        if operation == "retire":
            closed = store.memory.retire(room, memory.memory_id)
        else:
            closed = store.memory.close_open_loop(room, memory.memory_id, action="complete")
    assert closed.status == "retired"
    assert not store.memory.fts_index.status()["usable"]
    assert store.memory.retrieve(room, "文档链接") == ()
    restart(store)
    assert docs(store) == []


def test_failure_to_persist_dirty_marker_rolls_back_outer_write(setup, monkeypatch):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    before, projection = canonical(store), docs(store)

    def fail(*args):
        raise sqlite3.OperationalError("simulated metadata failure")

    monkeypatch.setattr(store.memory.fts_index, "_sync_one", fail)
    monkeypatch.setattr(store.memory.fts_index, "_mark", fail)
    with pytest.raises(sqlite3.OperationalError):
        store.memory.retire(room, memory.memory_id)
    assert canonical(store) == before
    assert docs(store) == projection
    assert store.memory.fts_index.status()["usable"]


def test_no_fts5_runtime_still_starts_and_uses_legacy(tmp_path, monkeypatch):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "legacy.sqlite3"))

    def unavailable(*args):
        raise sqlite3.OperationalError("no such module: fts5")

    with monkeypatch.context() as patch:
        patch.setattr(MemoryFTSIndex, "_create_schema", unavailable)
        store.initialize()
        assert store.memory.fts_index.status()["status"] == "unavailable"
        service = StandaloneChatService(store)
        room = service.create_room(title="legacy", idempotency_key=uuid4()).room_id
        memory = save(service, room, "标题使用前缀")
        assert store.memory.retrieve(room, "标题") == (memory,)
        assert docs(store) == []
    if store.memory.fts_index.initialize():
        assert matches(store, "标题") == {str(memory.memory_id)}


@pytest.mark.parametrize("damage", ["revision", "projection", "missing_table", "missing_metadata"])
def test_startup_repairs_derived_drift_without_changing_canonical(setup, damage):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    snapshot = canonical(store)
    with store.database.transaction() as conn:
        if damage == "revision":
            conn.execute("UPDATE standalone_chat_memory_fts_state SET revision='old'")
        elif damage == "projection":
            conn.execute("UPDATE standalone_chat_memory_fts_documents SET tokens='stale', room_id='wrong'")
        elif damage == "missing_metadata":
            conn.execute("DELETE FROM standalone_chat_memory_fts_state")
        else:
            conn.execute("DROP TABLE standalone_chat_memory_fts")
    restart(store)
    assert canonical(store) == snapshot
    assert matches(store, "标题") == {str(memory.memory_id)}
    assert docs(store)[0]["room_id"] == str(room)


def test_failed_rebuild_rolls_back_projection_and_recovers(setup, monkeypatch):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    snapshot, projection = canonical(store), docs(store)

    def fail(*args):
        raise FTSTextLimitError("simulated encoding limit")

    with monkeypatch.context() as patch:
        patch.setattr(store.memory.fts_index, "_document", fail)
        assert not store.memory.fts_index.initialize(force_rebuild=True)
    assert canonical(store) == snapshot
    assert docs(store) == projection
    assert store.memory.fts_index.status()["status"] == "dirty"
    restart(store)
    assert matches(store, "标题") == {str(memory.memory_id)}


def test_real_version_25_upgrade_backfills_only_active_memories(tmp_path, monkeypatch):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "upgrade.sqlite3"))
    store.database.initialize((*STANDALONE_CHAT_MIGRATIONS, *CHAT_MEMORY_MIGRATIONS[:-1]))
    service = StandaloneChatService(store)
    room = service.create_room(title="upgrade", idempotency_key=uuid4()).room_id
    with monkeypatch.context() as patch:
        patch.setattr(store.memory.fts_index, "sync", lambda *args: False)
        old = save(service, room, "数据库使用 SQLite")
        new = save(service, room, "数据库改用 PostgreSQL", replaces_memory_id=old.memory_id)
        retired = save(service, room, "标题使用前缀")
        store.memory.retire(room, retired.memory_id)
        msg = source(service, room, "下次发布前检查文档链接")
        todo = store.memory.capture_agent_proposal(room, msg.message_id,
            AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
        store.memory.close_open_loop(room, todo.memory_id, action="complete")
    before = canonical(store)
    store.initialize()
    assert canonical(store) == before
    if store.memory.fts_index.status()["usable"]:
        assert {row["memory_id"] for row in docs(store)} == {str(new.memory_id)}
        assert matches(store, "SQLite") == set()
        assert matches(store, "PostgreSQL") == {str(new.memory_id)}


def test_sync_requires_transaction_and_revision_match(setup):
    store, service, room = setup
    memory = save(service, room, "标题使用前缀")
    with store.database.connect() as conn, pytest.raises(ValueError, match="transaction"):
        store.memory.fts_index.sync(conn, memory.memory_id)
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memory_fts_state SET revision='other-writer'")
        assert not store.memory.fts_index.sync(conn, memory.memory_id)
    assert store.memory.fts_index.status()["error_type"] == "RevisionMismatch"
    restart(store)


def test_missing_metadata_during_write_explicitly_invalidates(setup):
    store, service, room = setup
    with store.database.transaction() as conn:
        conn.execute("DELETE FROM standalone_chat_memory_fts_state")
    memory = save(service, room, "标题使用前缀")
    assert store.memory.fts_index.status()["error_type"] == "MissingMetadata"
    assert docs(store) == []
    restart(store)
    assert matches(store, "标题") == {str(memory.memory_id)}
