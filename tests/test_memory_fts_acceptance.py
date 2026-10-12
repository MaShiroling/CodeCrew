"""Step 5 acceptance matrix; temporary DBs and fake embeddings only."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from app.chat.dispatch import chat_context
from app.chat.fts_memory import MemoryFTSIndex
from app.chat.fts_text import build_fts_query, encode_fts_document
from app.chat.memory import AgentMemoryAction, AgentMemoryProposal
from app.chat.models import ExternalChatSource, StandaloneChatMessage
from app.chat.service import ChatInvalid, StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import MemoryVectorIndex
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


class FakeEmbedder:
    """Constant vectors exercise wiring/state, never semantic quality."""

    def __init__(self, fingerprint="fake-v1", vector=(1.0, 0.0)):
        self.model_fingerprint = fingerprint
        self.vector = vector
        self.calls = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return tuple(self.vector for _ in texts)


@pytest.fixture(params=["legacy", "bm25"])
def setup(tmp_path, request):
    return make_setup(tmp_path, request.param)


def make_setup(tmp_path, backend):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "acceptance.sqlite3"),
                                lexical_backend=backend)
    store.initialize()
    if not store.memory.fts_index.status()["usable"]:
        pytest.skip("actual FTS5 runtime required for index acceptance")
    service = StandaloneChatService(store)
    room = service.create_room(title="acceptance", idempotency_key=uuid4()).room_id
    return store, service, room


def post(service, room, text):
    return service.post_message(room, content="@白金 " + text, idempotency_key=uuid4(),
                                reply_to=None).message


def save(service, room, text, **kwargs):
    message = post(service, room, text)
    return service.create_memory(room, source_message_id=message.message_id,
                                 content=text, **kwargs)


def snapshot(store):
    with store.database.connect() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("standalone_chat_memories", "standalone_chat_messages",
                              "standalone_chat_memory_candidates")}


def match_ids(store, query):
    with store.database.connect() as conn:
        return {row[0] for row in conn.execute(
            "SELECT doc.memory_id FROM standalone_chat_memory_fts "
            "JOIN standalone_chat_memory_fts_documents doc ON doc.rowid=standalone_chat_memory_fts.rowid "
            "WHERE standalone_chat_memory_fts MATCH ?", (build_fts_query(query),),
        )}


def match_rowids(store, query):
    # Unlike a projection JOIN, this reveals orphaned inverted-index entries.
    with store.database.connect() as conn:
        return {row[0] for row in conn.execute(
            "SELECT rowid FROM standalone_chat_memory_fts WHERE standalone_chat_memory_fts MATCH ?",
            (build_fts_query(query),),
        )}


def index_integrity(store):
    assert store.memory.fts_index.status()["usable"]
    with store.database.transaction() as conn:
        store.memory.fts_index._integrity_check(conn)


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("action", ["complete", "cancel"])
def test_full_lifecycle_after_process_restart_and_forced_rebuild(setup, hybrid, action):
    store, service, room = setup
    embedder = FakeEmbedder()
    if hybrid:
        store.memory.vector_index = MemoryVectorIndex(store.database, embedder)
    old = save(service, room, "数据库使用 SQLite")
    assert service.create_memory(room, source_message_id=old.source_message_id,
                                 content=old.content) == old
    assert store.memory.retrieve(room, "SQLite") == (old,)
    discussion = store.get_message(old.source_message_id).message.correlation_id
    assert store.memory.retrieve(room, "SQLite", exclude_correlation_id=discussion) == ()
    new = save(service, room, "数据库改用 PostgreSQL", replaces_memory_id=old.memory_id)
    assert store.memory.retrieve(room, "PostgreSQL") == (new,)
    assert match_ids(store, "SQLite") == set()
    assert match_ids(store, "PostgreSQL") == {str(new.memory_id)}
    store.memory.retire(room, new.memory_id)
    assert store.memory.retrieve(room, "PostgreSQL") == ()
    with pytest.raises(ValueError, match="retired"):
        store.memory.create(room, new.source_message_id, new.content,
                            replaces_memory_id=old.memory_id)
    message = post(service, room, "下次发布前检查文档链接")
    proposal = AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接")
    todo = store.memory.capture_agent_proposal(room, message.message_id, proposal)
    assert todo is not None
    assert store.memory.capture_agent_proposal(room, message.message_id, proposal) == todo
    assert store.memory.retrieve(room, "文档链接") == (todo,)
    text = "文档链接检查已经完成" if action == "complete" else "取消文档链接检查"
    evidence = post(service, room, text)
    closure = AgentMemoryAction(action=action, memory_id=todo.memory_id, evidence=text)
    closed = store.memory.capture_agent_action(room, evidence.message_id, closure)
    assert closed is not None and closed.closed_by_message_id == evidence.message_id
    assert store.memory.capture_agent_action(room, evidence.message_id, closure) == closed
    assert store.memory.capture_agent_proposal(room, message.message_id, proposal) == closed
    assert store.memory.retrieve(room, "文档链接") == ()
    before = snapshot(store)
    call_count = len(embedder.calls)
    restarted = StandaloneChatStore(SQLiteDatabase(store.database.path),
                                    lexical_backend=store.memory.lexical_backend)
    restarted.initialize()
    assert restarted.memory.fts_index.initialize(force_rebuild=True)
    if hybrid:
        restarted.memory.vector_index = MemoryVectorIndex(restarted.database, embedder)
    assert snapshot(restarted) == before
    assert len(embedder.calls) == call_count  # FTS startup/rebuild adds no embedding calls
    assert restarted.memory.retrieve(room, "文档链接 SQLite PostgreSQL") == ()
    assert restarted.memory.list_closed_open_loops(room) == (closed,)
    assert len(before["standalone_chat_memories"]) == 3
    index_integrity(restarted)
    # Validate maintenance after recovery, not merely the recovered snapshot.
    after = save(StandaloneChatService(restarted), room, "以后使用 MySQL")
    assert match_ids(restarted, "MySQL") == {str(after.memory_id)}


@pytest.mark.parametrize("trigger", ["ai", "ad", "au"])
@pytest.mark.parametrize("damage", ["missing", "noop"])
def test_startup_restores_own_triggers_and_future_updates(setup, trigger, damage):
    store, service, room = setup
    existing = save(service, room, "SQLite")
    before = snapshot(store)
    events = {"ai": "INSERT", "ad": "DELETE", "au": "UPDATE"}
    with store.database.transaction() as conn:
        name = f"standalone_chat_memory_fts_{trigger}"
        conn.execute(f"DROP TRIGGER {name}")
        if damage == "noop":
            conn.execute(f"CREATE TRIGGER {name} AFTER {events[trigger]} "
                         "ON standalone_chat_memory_fts_documents BEGIN SELECT 1; END")
    store.initialize()
    assert snapshot(store) == before
    added = save(service, room, "PostgreSQL")
    assert match_ids(store, "PostgreSQL") == {str(added.memory_id)}
    store.memory.retire(room, existing.memory_id)
    assert match_ids(store, "SQLite") == set()
    assert match_rowids(store, "SQLite") == set()
    # Direct projection update is an isolated fixture proving the update trigger;
    # it is not an API permitting changes to canonical facts.
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memory_fts_documents SET tokens=? WHERE memory_id=?",
                     (encode_fts_document("MySQL"), str(added.memory_id)))
    assert match_ids(store, "PostgreSQL") == set()
    assert match_ids(store, "MySQL") == {str(added.memory_id)}
    store.initialize()  # repair fixture-only projection drift from canonical truth
    assert match_ids(store, "PostgreSQL") == {str(added.memory_id)}
    index_integrity(store)


@pytest.mark.parametrize("damage", ["inverted_only", "missing_document", "extra_retired_document",
                                   "room", "discussion", "revision", "hash"])
def test_audit_repairs_drift_without_mutating_canonical_records(setup, damage):
    store, service, room = setup
    memory = save(service, room, "SQLite")
    retired = save(service, room, "PostgreSQL")
    with store.database.connect() as conn:
        retired_doc = dict(conn.execute("SELECT * FROM standalone_chat_memory_fts_documents "
                                        "WHERE memory_id=?", (str(retired.memory_id),)).fetchone())
    store.memory.retire(room, retired.memory_id)
    before = snapshot(store)
    with store.database.transaction() as conn:
        if damage == "inverted_only":
            conn.execute("INSERT INTO standalone_chat_memory_fts(standalone_chat_memory_fts) "
                         "VALUES('delete-all')")
        elif damage == "missing_document":
            conn.execute("DELETE FROM standalone_chat_memory_fts_documents WHERE memory_id=?",
                         (str(memory.memory_id),))
        elif damage == "extra_retired_document":
            columns = tuple(retired_doc)
            conn.execute(f"INSERT INTO standalone_chat_memory_fts_documents ({','.join(columns)}) "
                         f"VALUES ({','.join('?' for _ in columns)})", tuple(retired_doc.values()))
        else:
            column = {"room": "room_id", "discussion": "source_correlation_id",
                      "revision": "revision", "hash": "content_hash"}[damage]
            conn.execute(f"UPDATE standalone_chat_memory_fts_documents SET {column}='drift'")
    if damage == "inverted_only":
        with store.database.transaction() as conn, pytest.raises(sqlite3.DatabaseError):
            store.memory.fts_index._integrity_check(conn)
    store.initialize()
    assert snapshot(store) == before
    assert store.memory.retrieve(room, "SQLite") == (memory,)
    assert store.memory.retrieve(room, "PostgreSQL") == ()
    assert match_ids(store, "SQLite") == {str(memory.memory_id)}
    assert match_ids(store, "PostgreSQL") == set()
    index_integrity(store)


@pytest.mark.parametrize("failure", ["encoding", "integrity", "trigger"])
def test_mid_rebuild_failure_restores_derived_data_and_can_recover(setup, monkeypatch, failure):
    store, service, room = setup
    first = save(service, room, "SQLite")
    second = save(service, room, "PostgreSQL")
    before = snapshot(store)
    with store.database.connect() as conn:
        documents = [tuple(row) for row in conn.execute("SELECT * FROM standalone_chat_memory_fts_documents")]
        triggers = [tuple(row) for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    with monkeypatch.context() as patch:
        if failure == "encoding":
            real = store.memory.fts_index._document
            count = 0

            def fail(row):
                nonlocal count
                count += 1
                if count == 2:
                    raise sqlite3.OperationalError("synthetic mid-backfill failure")
                return real(row)

            patch.setattr(store.memory.fts_index, "_document", fail)
        elif failure == "integrity":
            def fail(conn):
                raise sqlite3.DatabaseError("synthetic rebuild integrity failure")

            patch.setattr(store.memory.fts_index, "_integrity_check", fail)
        else:
            real = store.memory.fts_index._create_triggers
            count = 0

            def fail(conn):
                nonlocal count
                count += 1
                real(conn)
                if count == 2:  # after rebuild, not initial schema preparation
                    raise sqlite3.OperationalError("synthetic post-rebuild trigger failure")

            patch.setattr(store.memory.fts_index, "_create_triggers", fail)
        assert not store.memory.fts_index.initialize(force_rebuild=True)
    assert snapshot(store) == before
    assert store.memory.fts_index.status()["status"] == "dirty"
    with store.database.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM standalone_chat_memory_fts_documents")] == documents
        assert [tuple(row) for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == triggers
    assert store.memory.retrieve(room, "SQLite") == (first,)
    assert store.memory.fts_index.initialize(force_rebuild=True)
    assert store.memory.retrieve(room, "PostgreSQL") == (second,)
    assert match_ids(store, "SQLite") == {str(first.memory_id)}
    third = save(service, room, "MySQL")
    assert match_ids(store, "MySQL") == {str(third.memory_id)}
    index_integrity(store)


@pytest.mark.parametrize("which", ["root", "local_reply"])
def test_external_discussions_never_invoke_either_retrieval_backend(setup, monkeypatch, which):
    store, service, room_id = setup
    room = store.get_room(room_id)
    human = next(member for member in room.members if member.role is MemberRole.HUMAN)
    planner = next(member for member in room.members if member.role is MemberRole.PLANNER)
    memory = save(service, room_id, "内部代号是海棠计划")
    message = post(service, room_id, "下次发布前检查文档链接")
    todo = store.memory.capture_agent_proposal(room_id, message.message_id,
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
    external = store.append_message(StandaloneChatMessage(
        room_id=room_id, trace_id=room.trace_id, sender_id=human.member_id,
        recipient_ids=(planner.member_id,), content="海棠计划；文档链接检查已经完成",
        idempotency_key=str(uuid4()), external_source=ExternalChatSource(
            external_chat_id="fixture-chat", external_sender_id="fixture-user"),
    ))
    query = external
    if which == "local_reply":
        query = store.append_message(StandaloneChatMessage(
            room_id=room_id, trace_id=room.trace_id, sender_id=human.member_id,
            recipient_ids=(planner.member_id,), content="海棠计划；文档链接检查已经完成",
            idempotency_key=str(uuid4()), reply_to=external.message.message_id,
            correlation_id=external.message.correlation_id,
        ))
    before = snapshot(store)
    monkeypatch.setattr(store.memory, "retrieve", lambda *args, **kwargs: pytest.fail("external recall"))
    context = json.loads(chat_context(store, query, room))
    assert context["room_memories"] == []
    assert memory.content not in json.dumps(context, ensure_ascii=False)
    with pytest.raises(ChatInvalid):
        service.create_memory(room_id, source_message_id=query.message.message_id, content="海棠计划")
    assert store.memory.capture_agent_proposal(room_id, query.message.message_id,
        AgentMemoryProposal(kind="decision", content="海棠计划")) is None
    assert store.memory.capture_agent_action(room_id, query.message.message_id,
        AgentMemoryAction(action="complete", memory_id=todo.memory_id,
                          evidence="文档链接检查已经完成")) is None
    assert snapshot(store) == before
    index_integrity(store)


@pytest.mark.parametrize("mutation", ["retire", "rebuild"])
def test_fts_read_and_canonical_candidates_share_snapshot(tmp_path, monkeypatch, mutation):
    store, service, room = make_setup(tmp_path, "bm25")
    memory = save(service, room, "SQLite")
    real = store.memory.fts_index.search
    observed = []

    def search(conn, *args, **kwargs):
        # Canonical candidate SELECT already established the read snapshot.
        # A separate writer commits a retirement while this read is in progress.
        writer = StandaloneChatStore(SQLiteDatabase(store.database.path), lexical_backend="bm25")
        if mutation == "retire":
            writer.memory.retire(room, memory.memory_id)
        else:
            assert writer.memory.fts_index.initialize(force_rebuild=True)
        result = real(conn, *args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(store.memory.fts_index, "search", search)
    result = store.memory.retrieve(room, "SQLite")
    assert observed == [(memory.memory_id,)]
    assert result == (() if mutation == "retire" else (memory,))


@pytest.mark.parametrize("mutation", ["retire", "rebuild"])
def test_real_parallel_writer_during_blocked_vector_search(setup, mutation):
    store, service, room = setup
    memory = save(service, room, "SQLite")
    entered, release = Event(), Event()

    class BlockingVector:
        def search(self, query, memories):
            entered.set()
            assert release.wait(5), "test writer did not release vector search"
            return ((memory.memory_id, 0.99),)

    store.memory.vector_index = BlockingVector()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.memory.retrieve, room, "SQLite")
        try:
            assert entered.wait(5), "reader never reached vector stage"
            writer = StandaloneChatStore(SQLiteDatabase(store.database.path))
            if mutation == "retire":
                writer.memory.retire(room, memory.memory_id)
            else:
                before = snapshot(writer)
                assert writer.memory.fts_index.initialize(force_rebuild=True)
                assert snapshot(writer) == before
        finally:
            release.set()
        assert future.result(timeout=5) == (() if mutation == "retire" else (memory,))
    index_integrity(store)


def test_model_revision_and_dimension_drift_keep_lexical_backend_available(setup):
    store, service, room = setup
    memory = save(service, room, "SQLite")
    v1 = FakeEmbedder()
    store.memory.vector_index = MemoryVectorIndex(store.database, v1)
    assert store.memory.retrieve(room, "SQLite") == (memory,)
    v2 = FakeEmbedder("fake-v2", (1.0, 0.0, 0.0))
    store.memory.vector_index = MemoryVectorIndex(store.database, v2)
    assert store.memory.retrieve(room, "SQLite") == (memory,)
    with store.database.connect() as conn:
        row = conn.execute("SELECT model_fingerprint,dimensions FROM standalone_chat_memory_embeddings "
                           "WHERE memory_id=?", (str(memory.memory_id),)).fetchone()
    assert tuple(row) == ("fake-v2", 3)
    # Same identity with changed dimensions is an error, not silently mixed.
    v2.vector = (1.0, 0.0)
    with store.database.transaction() as conn:
        conn.execute("DELETE FROM standalone_chat_memory_embeddings")
    assert store.memory.retrieve(room, "SQLite") == (memory,)
    assert store.memory.fts_index.status()["usable"]


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("operation", ["create", "replace", "retire", "complete", "cancel"])
def test_failed_lifecycle_sync_degrades_then_rebuilds_current_truth(
    setup, monkeypatch, caplog, hybrid, operation,
):
    store, service, room = setup
    if hybrid:
        store.memory.vector_index = MemoryVectorIndex(store.database, FakeEmbedder())
    old = save(service, room, "SQLite")
    todo = None
    if operation in ("complete", "cancel"):
        message = post(service, room, "下次发布前检查文档链接")
        todo = store.memory.capture_agent_proposal(room, message.message_id,
            AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
    with store.database.connect() as conn:
        projection = [tuple(row) for row in conn.execute("SELECT * FROM standalone_chat_memory_fts_documents")]
    real = store.memory.fts_index._sync_one

    def fail(conn, memory_id):
        real(conn, memory_id)
        raise sqlite3.OperationalError("fixture-private-marker")

    with monkeypatch.context() as patch:
        patch.setattr(store.memory.fts_index, "_sync_one", fail)
        if operation == "create":
            save(service, room, "PostgreSQL")
        elif operation == "replace":
            save(service, room, "PostgreSQL", replaces_memory_id=old.memory_id)
        elif operation == "retire":
            store.memory.retire(room, old.memory_id)
        else:
            store.memory.close_open_loop(room, todo.memory_id, action=operation)
    assert store.memory.fts_index.status()["status"] == "dirty"
    assert "fixture-private-marker" not in caplog.text
    with store.database.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM standalone_chat_memory_fts_documents")] == projection
    expected = {memory.memory_id for memory in store.memory.list_active(room)}
    query = "SQLite PostgreSQL 文档链接"
    assert {memory.memory_id for memory in store.memory.retrieve(room, query)} == expected
    before = snapshot(store)
    assert store.memory.fts_index.initialize(force_rebuild=True)
    assert snapshot(store) == before
    assert {memory.memory_id for memory in store.memory.retrieve(room, query)} == expected
    assert match_ids(store, query) == {str(memory_id) for memory_id in expected}
    index_integrity(store)


@pytest.mark.parametrize("backend", ["legacy", "bm25"])
def test_absent_fts_runtime_can_write_replace_close_and_recall(tmp_path, monkeypatch, backend):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "no-fts.sqlite3"), lexical_backend=backend)

    def unavailable(*args):
        raise sqlite3.OperationalError("no such module: fts5")

    with monkeypatch.context() as patch:
        patch.setattr(MemoryFTSIndex, "_create_schema", unavailable)
        store.initialize()
        service = StandaloneChatService(store)
        room = service.create_room(title="no FTS", idempotency_key=uuid4()).room_id
        old = save(service, room, "SQLite")
        new = save(service, room, "PostgreSQL", replaces_memory_id=old.memory_id)
        assert store.memory.retrieve(room, "SQLite") == ()
        assert store.memory.retrieve(room, "PostgreSQL") == (new,)
        store.memory.retire(room, new.memory_id)
        message = post(service, room, "下次发布前检查文档链接")
        todo = store.memory.capture_agent_proposal(room, message.message_id,
            AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))
        store.memory.close_open_loop(room, todo.memory_id, action="cancel")
        assert store.memory.retrieve(room, "文档链接") == ()
        active = save(service, room, "MySQL")
        assert store.memory.fts_index.status()["status"] == "unavailable"
    before = snapshot(store)
    store.initialize()
    assert snapshot(store) == before
    assert store.memory.retrieve(room, "MySQL") == (active,)
    if store.memory.fts_index.status()["usable"]:
        assert match_ids(store, "MySQL") == {str(active.memory_id)}
        assert match_ids(store, "SQLite PostgreSQL 文档链接") == set()
