"""Opt-in BM25 recall and RRF integration, without network/model calls."""

import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.chat.fts_text import build_fts_query
from app.chat.memory import AgentMemoryProposal, fuse_memory_rankings
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import MemoryVectorIndex
from app.cli import _build_chat_service
from app.config import Settings
from app.storage import SQLiteDatabase


@pytest.fixture
def setup(tmp_path):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "bm25.sqlite3"), lexical_backend="bm25")
    store.initialize()
    if not store.memory.fts_index.status()["usable"]:
        pytest.skip("actual FTS5 runtime required")
    service = StandaloneChatService(store)
    room = service.create_room(title="BM25", idempotency_key=uuid4()).room_id
    return store, service, room


def source(service, room, text):
    return service.post_message(room, content="@白金 " + text,
                                idempotency_key=uuid4(), reply_to=None).message


def save(service, room, text, **kwargs):
    message = source(service, room, text)
    return service.create_memory(room, source_message_id=message.message_id, content=text, **kwargs)


def lexical(store, room, query, ids, exclude=None):
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        return store.memory.fts_index.search(conn, room, query, memory_ids=tuple(ids),
                                              exclude_correlation_id=exclude)


class StubVector:
    def __init__(self, ranking=(), callback=None, fail=False):
        self.ranking = ranking
        self.callback = callback
        self.fail = fail
        self.calls = []

    def search(self, query, memories):
        self.calls.append((query, tuple(memory.memory_id for memory in memories)))
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        return self.ranking


def test_actual_bm25_orders_smaller_score_first_not_legacy_recency(setup):
    store, service, room = setup
    strong = save(service, room, "gateway gateway gateway")
    weak = save(service, room, "gateway audit audit audit audit audit audit audit")
    ids = (strong.memory_id, weak.memory_id)
    with store.database.connect() as conn:
        scores = conn.execute("SELECT doc.memory_id, bm25(standalone_chat_memory_fts) score "
            "FROM standalone_chat_memory_fts JOIN standalone_chat_memory_fts_documents doc "
            "ON doc.rowid=standalone_chat_memory_fts.rowid "
            "WHERE standalone_chat_memory_fts MATCH ? ORDER BY score ASC", (build_fts_query("gateway"),)
        ).fetchall()
    assert scores[0]["score"] < scores[1]["score"] < 0
    assert lexical(store, room, "gateway", ids) == ids
    assert store.memory.retrieve(room, "gateway") == (strong, weak)
    legacy = StandaloneChatStore(store.database, lexical_backend="legacy")
    assert legacy.memory.retrieve(room, "gateway") == (weak, strong)


@pytest.mark.parametrize("query", ["@月见 C++", "Ｃ＋＋", 'C++ OR "*"', "C++'); DROP TABLE x;--"])
def test_literal_query_and_technical_tokens(setup, query):
    store, service, room = setup
    target = save(service, room, "使用 C++")
    save(service, room, "使用 Python")
    assert store.memory.retrieve(room, query) == (target,)
    assert store.memory.fts_index.status()["usable"]


def test_successful_empty_bm25_is_not_legacy_fallback(setup, monkeypatch):
    store, service, room = setup
    save(service, room, "foo-bar")
    # Legacy terms split foo-bar, but the FTS rule keeps the query's atomic word.
    assert store.memory.retrieve(room, "foobar") == ()
    monkeypatch.setattr(store.memory.fts_index, "search", lambda *args, **kwargs: ())
    assert store.memory.retrieve(room, "foo") == ()
    assert store.memory.fts_index.status()["usable"]


@pytest.mark.parametrize("query", ["", "@月见", "！？", "的"])
def test_empty_token_queries_do_not_run_match(setup, monkeypatch, query):
    store, service, room = setup
    save(service, room, "项目使用 SQLite")
    statements = []
    real_open = store.database._open_connection

    def opened():
        conn = real_open()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store.database, "_open_connection", opened)
    assert store.memory.retrieve(room, query) == ()
    assert not any(" MATCH " in sql for sql in statements)


def test_room_current_discussion_and_canonical_status_filtered_inside_search(setup):
    store, service, room = setup
    allowed = save(service, room, "gateway target")
    current = save(service, room, "gateway current")
    retired = save(service, room, "gateway retired")
    other_room = service.create_room(title="other", idempotency_key=uuid4()).room_id
    other = save(service, other_room, "gateway gateway gateway")
    discussion = store.get_message(current.source_message_id).message.correlation_id
    # Simulate stale projection after a canonical-only retirement. It must not
    # become a result even if its old inverted-index entry remains.
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET status='retired' WHERE memory_id=?",
                     (str(retired.memory_id),))
    ids = (allowed.memory_id, current.memory_id, retired.memory_id, other.memory_id)
    assert lexical(store, room, "gateway", ids, discussion) == (allowed.memory_id,)
    assert store.memory.retrieve(room, "gateway", exclude_correlation_id=discussion) == (allowed,)


def test_rrf_uses_full_lexical_ranking_before_return_limit(setup):
    store, service, room = setup
    items = [save(service, room, "gateway " + "audit " * size) for size in (1, 3, 6, 9)]
    assert lexical(store, room, "gateway", [item.memory_id for item in items]) == tuple(
        item.memory_id for item in items)
    vector = StubVector(((items[3].memory_id, 0.99),))
    store.memory.vector_index = vector
    result = store.memory.retrieve(room, "gateway", limit=1)
    assert result == (items[3],)  # fourth lexical + first semantic beats lexical-only first
    assert set(vector.calls[0][1]) == {item.memory_id for item in items}


def test_semantic_only_candidates_and_lexical_only_candidates_both_survive(setup):
    store, service, room = setup
    lexical_item = save(service, room, "gateway")
    semantic_item = save(service, room, "异步任务使用消息队列")
    vector = StubVector(((semantic_item.memory_id, 0.99), (uuid4(), 1.0)))
    store.memory.vector_index = vector
    assert store.memory.retrieve(room, "gateway") == (lexical_item, semantic_item)
    assert store.memory.retrieve(room, "无词项重合的意图") == (semantic_item,)


def test_real_vector_threshold_keeps_only_qualified_semantic_hit(setup):
    store, service, room = setup
    good = save(service, room, "异步任务使用消息队列")
    save(service, room, "默认只喝温水")

    class FakeEmbedder:
        model_fingerprint = "offline-bm25-vector-v1"

        def embed(self, texts):
            vectors = {"异步任务使用消息队列": (1.0, 0.0), "默认只喝温水": (0.0, 1.0),
                       "后台作业如何解耦": (1.0, 0.0)}
            return tuple(vectors[text] for text in texts)

    store.memory.vector_index = MemoryVectorIndex(store.database, FakeEmbedder(), min_similarity=0.65)
    assert store.memory.retrieve(room, "后台作业如何解耦") == (good,)


def test_vector_failure_falls_back_to_bm25_not_legacy(setup):
    store, service, room = setup
    strong = save(service, room, "gateway gateway gateway")
    weak = save(service, room, "gateway audit audit audit audit audit")
    store.memory.vector_index = StubVector(fail=True)
    assert store.memory.retrieve(room, "gateway") == (strong, weak)
    assert store.memory.fts_index.status()["usable"]


@pytest.mark.parametrize("status", ["dirty", "unavailable", "revision", "missing_metadata"])
def test_untrusted_index_uses_legacy_without_match(setup, monkeypatch, status):
    store, service, room = setup
    first = save(service, room, "gateway gateway gateway")
    recent = save(service, room, "gateway audit audit audit audit")
    with store.database.transaction() as conn:
        if status == "missing_metadata":
            conn.execute("DELETE FROM standalone_chat_memory_fts_state")
        elif status == "revision":
            conn.execute("UPDATE standalone_chat_memory_fts_state SET revision='old'")
        else:
            conn.execute("UPDATE standalone_chat_memory_fts_state SET status=?", (status,))
    statements = []
    real_open = store.database._open_connection

    def opened():
        conn = real_open()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store.database, "_open_connection", opened)
    assert store.memory.retrieve(room, "gateway") == (recent, first)
    assert not any(" MATCH " in sql for sql in statements)


def test_missing_fts_table_marks_dirty_falls_back_and_restart_recovers(setup):
    store, service, room = setup
    first = save(service, room, "gateway gateway gateway")
    recent = save(service, room, "gateway audit audit audit audit")
    with store.database.transaction() as conn:
        conn.execute("DROP TABLE standalone_chat_memory_fts")
    assert store.memory.retrieve(room, "gateway") == (recent, first)
    assert store.memory.fts_index.status()["status"] == "dirty"
    assert store.memory.fts_index.status()["error_type"] == "OperationalError"
    store.initialize()
    assert store.memory.retrieve(room, "gateway") == (first, recent)


def test_query_limit_falls_back_without_invalidating_healthy_index(setup):
    store, service, room = setup
    memory = save(service, room, "gateway")
    assert store.memory.retrieve(room, "gateway " * 200) == (memory,)
    assert store.memory.fts_index.status()["usable"]


def test_both_branches_fail_fall_back_to_legacy(setup, monkeypatch):
    store, service, room = setup
    memory = save(service, room, "gateway")

    def fail(*args, **kwargs):
        raise sqlite3.DatabaseError("synthetic failure")

    monkeypatch.setattr(store.memory.fts_index, "search", fail)
    store.memory.vector_index = StubVector(fail=True)
    assert store.memory.retrieve(room, "gateway") == (memory,)
    assert store.memory.fts_index.status()["status"] == "dirty"


def test_fts_failure_keeps_vector_branch_available(setup, monkeypatch):
    store, service, room = setup
    memory = save(service, room, "默认喝温水")

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(store.memory.fts_index, "search", fail)
    store.memory.vector_index = StubVector(((memory.memory_id, 0.99),))
    assert store.memory.retrieve(room, "飲用習慣") == (memory,)


@pytest.mark.parametrize("backend", ["legacy", "bm25"])
@pytest.mark.parametrize("mutation", ["retire", "replace", "close"])
def test_final_canonical_validation_drops_memory_changed_during_vector_search(setup, backend, mutation):
    store, service, room = setup
    store.memory.lexical_backend = backend
    message = source(service, room, "下次发布前检查文档链接")
    memory = store.memory.capture_agent_proposal(room, message.message_id,
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"))

    def mutate():
        if mutation == "retire":
            store.memory.retire(room, memory.memory_id)
        elif mutation == "close":
            store.memory.close_open_loop(room, memory.memory_id, action="complete")
        else:
            store.memory.vector_index = None
            save(service, room, "以后检查数据库备份", replaces_memory_id=memory.memory_id)

    store.memory.vector_index = StubVector(((memory.memory_id, 0.99),), callback=mutate)
    assert store.memory.retrieve(room, "文档链接") == ()


def test_sensitive_query_and_memories_not_sent_to_vector_branch(setup):
    store, service, room = setup
    secret = save(service, room, "password: synthetic-not-real")
    safe = save(service, room, "gateway")
    vector = StubVector(((safe.memory_id, 0.99),))
    store.memory.vector_index = vector
    assert store.memory.retrieve(room, "password") == (secret,)
    assert vector.calls == []
    store.memory.retrieve(room, "gateway")
    assert vector.calls[0][1] == (safe.memory_id,)


@pytest.mark.parametrize("limit", [0, 4, -1])
def test_invalid_recall_limits_rejected(setup, limit):
    store, _, room = setup
    with pytest.raises(ValueError, match="recall limit"):
        store.memory.retrieve(room, "gateway", limit=limit)


def test_equal_scores_have_deterministic_recency_id_ties(setup):
    store, service, room = setup
    items = [save(service, room, "gateway") for _ in range(3)]
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET created_at='2026-10-11T00:00:00+00:00'")
    expected = tuple(sorted((item.memory_id for item in items), key=str, reverse=True))
    assert lexical(store, room, "gateway", expected) == expected
    assert tuple(item.memory_id for item in store.memory.retrieve(room, "gateway")) == expected


def test_search_requires_snapshot_and_bounded_candidate_set(setup):
    store, _, room = setup
    with store.database.connect() as conn:
        with pytest.raises(ValueError, match="snapshot"):
            store.memory.fts_index.search(conn, room, "gateway", memory_ids=())
        conn.execute("BEGIN")
        with pytest.raises(ValueError, match="limit"):
            store.memory.fts_index.search(conn, room, "gateway", memory_ids=tuple(uuid4() for _ in range(201)))


def test_search_never_expands_callers_candidate_universe(setup):
    store, service, room = setup
    strong = save(service, room, "gateway gateway gateway")
    weak = save(service, room, "gateway audit audit audit audit")
    assert lexical(store, room, "gateway", (weak.memory_id,)) == (weak.memory_id,)
    assert strong.memory_id not in lexical(store, room, "gateway", (weak.memory_id,))


def test_filters_are_applied_before_final_top_k(setup):
    store, service, room = setup
    target = save(service, room, "gateway audit audit audit audit")
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id
    for _ in range(5):
        save(service, other, "gateway gateway gateway")
    for _ in range(5):
        memory = save(service, room, "gateway gateway gateway")
        store.memory.retire(room, memory.memory_id)
    assert store.memory.retrieve(room, "gateway", limit=1) == (target,)


def test_two_hundred_candidate_boundary_and_three_result_limit(setup, monkeypatch):
    store, service, room = setup
    for i in range(200):
        save(service, room, f"gateway item{i}")
    real_search = store.memory.fts_index.search
    observed = []

    def search(*args, **kwargs):
        observed.append(len(kwargs["memory_ids"]))
        return real_search(*args, **kwargs)

    monkeypatch.setattr(store.memory.fts_index, "search", search)
    assert len(store.memory.retrieve(room, "gateway")) == 3
    assert observed == [200]
    with pytest.raises(ValueError, match="200"):
        store.memory.create(room, source(service, room, "gateway overflow").message_id,
                            "gateway overflow")


def test_failed_query_invalidation_still_falls_back_without_logging_private_text(setup, monkeypatch, caplog):
    store, service, room = setup
    memory = save(service, room, "gateway synthetic-private-query")

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic-private-query")

    monkeypatch.setattr(store.memory.fts_index, "search", fail)
    monkeypatch.setattr(store.memory.fts_index, "invalidate", fail)
    assert store.memory.retrieve(room, "gateway synthetic-private-query") == (memory,)
    assert "OperationalError" in caplog.text
    assert "synthetic-private-query" not in caplog.text


def test_fusion_filters_untrusted_ids_and_preserves_legacy_tie_policy(setup):
    _, service, room = setup
    first = save(service, room, "gateway")
    second = save(service, room, "audit")
    result = fuse_memory_rankings((first, second), (uuid4(), first.memory_id, first.memory_id),
                                  ((second.memory_id, 0.99),), limit=3)
    assert result == (first, second)


@pytest.mark.parametrize("backend", ["legacy", "bm25"])
def test_settings_and_cli_wire_backend_without_cloud(tmp_path, backend):
    settings = Settings(_env_file=None, memory_embedding_mode="off", memory_lexical_backend=backend,
                        database_url=f"sqlite:///{tmp_path / 'config.sqlite3'}")
    service = _build_chat_service(settings)
    assert service.store.memory.lexical_backend == backend
    assert service.store.memory.vector_index is None
    assert Settings(_env_file=None, memory_embedding_mode="off").memory_lexical_backend == "legacy"


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, memory_lexical_backend="unknown")
    with pytest.raises(ValueError, match="backend"):
        StandaloneChatStore(SQLiteDatabase(tmp_path / "bad.sqlite3"), lexical_backend="unknown")
