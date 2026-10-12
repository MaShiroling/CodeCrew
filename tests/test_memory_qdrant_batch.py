"""Batch shape/parity and WAL snapshot guards; no cloud or actual remote calls."""

import json
import sqlite3
import struct
from uuid import uuid4

import pytest

from evals.qdrant_snapshot_reference import rowwise_snapshot
from tests.test_memory_qdrant_recall import activate, save
from tests.test_memory_qdrant_recall import (
    setup as setup,  # noqa: PLC0414 - register the shared pytest fixture
)


def currents(conn):
    return tuple(conn.execute("SELECT * FROM standalone_chat_vector_revisions ORDER BY memory_id"))


def parity(setup):
    store, *_, worker = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        rows = currents(conn)
        expected = {row["memory_id"]: worker.desired_in_snapshot(conn, row) for row in rows}
        statements = []
        conn.set_trace_callback(lambda sql: statements.append(sql.split(None, 1)[0]))
        actual = worker.desired_many_in_snapshot(conn, tuple(reversed(rows)))
        conn.set_trace_callback(None)
    assert actual == expected
    assert all(token == "SELECT" for token in statements) and len(statements) <= 2
    return actual


@pytest.mark.parametrize("count", [1, 32, 200])
def test_constant_sql_bounded_bindings_and_input_order(setup, count):
    store, _, room, backend, embedder, index, worker = setup
    memories = tuple(save(setup, f"规范候选 {i}") for i in range(count))
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 200)
        rows = currents(conn)
        statements = []
        conn.set_trace_callback(lambda sql: statements.append(sql.split(None, 1)[0]))
        mutations = worker.desired_many_in_snapshot(conn, rows)
        conn.set_trace_callback(None)
        assert statements == ["SELECT", "SELECT"]
        assert len(mutations) == count and all(value[0].record for value in mutations.values())
    with store.database.connect() as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 200)
        statements = []
        conn.set_trace_callback(lambda sql: statements.append(sql.split(None, 1)[0]))
        _, selected = index._snapshot(conn, tuple(reversed(memories)), cached_only=True, room_id=room)
        assert statements.count("SELECT") == 5 and statements.count("BEGIN") == 1
        assert [item[0].memory_id for item in selected] == [m.memory_id for m in reversed(memories)]
    with store.database.connect() as conn:
        _, control = rowwise_snapshot(index, conn, tuple(reversed(memories)), cached_only=True, room_id=room)
    assert control == selected
    assert backend.calls == embedder.calls == []


@pytest.mark.parametrize("kind", ["missing", "model", "dimensions", "hash", "blob", "text_blob", "nan", "inf", "zero"])
def test_batch_matches_delivery_for_invalid_cache(setup, kind):
    memory = save(setup, "缓存忠实校验")
    store = setup[0]
    with store.database.transaction() as conn:
        if kind == "missing":
            conn.execute("DELETE FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(memory.memory_id),))
        else:
            column, value = {"model": ("model_fingerprint", "other"), "dimensions": ("dimensions", 3),
                "hash": ("content_hash", "b" * 64), "blob": ("vector", b"bad"), "text_blob": ("vector", "bad"),
                "nan": ("vector", struct.pack("<2f", float("nan"), 0)),
                "inf": ("vector", struct.pack("<2f", float("inf"), 0)),
                "zero": ("vector", struct.pack("<2f", 0, 0))}[kind]
            conn.execute(f"UPDATE standalone_chat_memory_embeddings SET {column}=? WHERE memory_id=?",
                         (value, str(memory.memory_id)))
    mutation, waiting = parity(setup)[str(memory.memory_id)]
    assert mutation.record is None and waiting in ("vector_missing_or_stale", "vector_invalid")
    assert setup[-2].search("其他措辞", (memory,)) == ()
    assert setup[3].calls == setup[4].calls == []


@pytest.mark.parametrize("change", ["external", "sensitive_source", "sensitive_fact", "agent", "foreign_member", "foreign_source"])
def test_batch_source_guards_match_delivery(setup, change):
    store, service, room, _, _, index, _ = setup
    memory = save(setup, "安全来源")
    with store.database.transaction() as conn:
        if change == "external":
            conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,'$.external_source','{}') "
                         "WHERE message_id=?", (str(memory.source_message_id),))
        elif change == "sensitive_source":
            conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,'$.content',?) "
                         "WHERE message_id=?", ("API key: synthetic-private", str(memory.source_message_id)))
        elif change == "sensitive_fact":
            conn.execute("UPDATE standalone_chat_memories SET content=? WHERE memory_id=?",
                         ("API key: synthetic-private", str(memory.memory_id)))
        elif change == "agent":
            member = conn.execute("SELECT member_id FROM standalone_chat_members WHERE room_id=? AND role='planner'",
                                  (str(room),)).fetchone()[0]
            conn.execute("UPDATE standalone_chat_messages SET sender_id=? WHERE message_id=?",
                         (member, str(memory.source_message_id)))
    if change.startswith("foreign"):
        other = service.create_room(title="other", idempotency_key=uuid4()).room_id
        with store.database.transaction() as conn:
            if change == "foreign_member":
                member = conn.execute("SELECT member_id FROM standalone_chat_members WHERE room_id=? AND role='human'",
                                      (str(other),)).fetchone()[0]
                conn.execute("UPDATE standalone_chat_messages SET sender_id=? WHERE message_id=?",
                             (member, str(memory.source_message_id)))
            else:
                conn.execute("UPDATE standalone_chat_messages SET room_id=? WHERE message_id=?",
                             (str(other), str(memory.source_message_id)))
    mutation, waiting = parity(setup)[str(memory.memory_id)]
    assert mutation.record is None and waiting is None
    assert index.search("其他措辞", (memory,)) == ()


@pytest.mark.parametrize("kind", ["external", "foreign", "earliest_local"])
def test_root_lookup_keeps_earliest_global_sequence_not_room_filtered(setup, kind):
    store, service, room, *_ = setup
    other = service.create_room(title="other", idempotency_key=uuid4()).room_id
    first = save(setup, "根消息", room=other if kind == "foreign" else room)
    later = save(setup, "之后的局部事实")
    first_source = store.get_message(first.source_message_id).message
    with store.database.transaction() as conn:
        if kind == "external":
            conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,'$.external_source','{}') "
                         "WHERE message_id=?", (str(first.source_message_id),))
        conn.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?",
                     (str(first_source.correlation_id), str(later.source_message_id)))
    result = parity(setup)[str(later.memory_id)]
    assert bool(result[0].record) is (kind == "earliest_local")


@pytest.mark.parametrize("change", ["retire", "missing_memory", "wrong_room"])
def test_tombstones_missing_rows_and_wrong_room_match_delivery(setup, change):
    store, _, room, *_, worker = setup
    memory = save(setup, "旧记录")
    if change == "retire":
        store.memory.retire(room, memory.memory_id)
    elif change == "missing_memory":
        with store.database.transaction() as conn:
            conn.execute("DELETE FROM standalone_chat_memory_embeddings WHERE memory_id=?", (str(memory.memory_id),))
            conn.execute("DELETE FROM standalone_chat_memory_fts_documents WHERE memory_id=?", (str(memory.memory_id),))
            conn.execute("DELETE FROM standalone_chat_memories WHERE memory_id=?", (str(memory.memory_id),))
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = dict(currents(conn)[0])
        if change == "wrong_room":
            row["room_id"] = str(uuid4())
        expected = worker.desired_in_snapshot(conn, row)
        assert worker.desired_many_in_snapshot(conn, (row,))[str(memory.memory_id)] == expected
        assert expected[0].record is None and expected[1] is None


@pytest.mark.parametrize("where", ["source", "root"])
def test_malformed_evidence_fails_same_as_delivery(setup, where):
    store, _, _, *_, worker = setup
    first = save(setup, "根消息")
    later = save(setup, "后续事实")
    root = store.get_message(first.source_message_id).message
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_messages SET correlation_id=? WHERE message_id=?",
                     (str(root.correlation_id), str(later.source_message_id)))
        target = first if where == "root" else later
        conn.execute("UPDATE standalone_chat_messages SET message_json='{' WHERE message_id=?",
                     (str(target.source_message_id),))
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                           (str(later.memory_id),)).fetchone()
        with pytest.raises(json.JSONDecodeError):
            worker.desired_in_snapshot(conn, row)
        with pytest.raises(json.JSONDecodeError):
            worker.desired_many_in_snapshot(conn, (row,))


@pytest.mark.parametrize("phase", ["canonical", "root"])
def test_wal_writer_between_batch_queries_does_not_mix_snapshots(setup, phase):
    store, _, room, _, _, index, _ = setup
    memory = save(setup, "必须保持读快照")
    class HookConnection:
        def __init__(self, conn):
            self.conn, self.fired = conn, False
        @property
        def in_transaction(self):
            return self.conn.in_transaction
        def execute(self, sql, params=()):
            target = "LEFT JOIN standalone_chat_memory_embeddings" if phase == "canonical" else "GROUP BY correlation_id"
            if target in sql and not self.fired:
                self.fired = True
                store.memory.retire(room, memory.memory_id)  # Independent WAL writer commits.
            return self.conn.execute(sql, params)
    with store.database.connect() as conn:
        wrapped = HookConnection(conn)
        _, selected = index._snapshot(wrapped, (memory,), cached_only=True, room_id=room)
        assert wrapped.fired and len(selected) == 1 and selected[0][1].record is not None
    with store.database.connect() as conn:
        _, selected = index._snapshot(conn, (memory,), cached_only=True, room_id=room)
        assert selected == ()  # Fresh post-query snapshot must see the retirement.


def test_excluded_corrupt_candidate_not_decoded_by_batch(setup):
    store, _, room, _, _, index, _ = setup
    allowed = save(setup, "可信事实")
    excluded = save(setup, "当前讨论排除")
    correlation = store.get_message(excluded.source_message_id).message.correlation_id
    activate(setup)
    with store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_messages SET message_json='{' WHERE message_id=?",
                     (str(excluded.source_message_id),))
    hits = index.search_scoped("其他措辞", (excluded, allowed), room_id=room, exclude_correlation_id=correlation)
    assert [hit[0] for hit in hits] == [allowed.memory_id]


def test_empty_batch_does_no_sql_and_requires_snapshot(setup):
    store, *_, worker = setup
    with store.database.connect() as conn:
        with pytest.raises(ValueError, match="snapshot"):
            worker.desired_many_in_snapshot(conn, ())
        conn.execute("BEGIN")
        queries = []
        conn.set_trace_callback(queries.append)
        assert worker.desired_many_in_snapshot(conn, ()) == {} and not queries


@pytest.mark.parametrize("kind", ["duplicate", "oversize", "invalid_id"])
def test_invalid_batches_reject_before_queries(setup, kind):
    memory = save(setup, "边界")
    store, *_, worker = setup
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = dict(currents(conn)[0])
        batch = (row, row) if kind == "duplicate" else (row,) * 201 if kind == "oversize" else (
            {**row, "memory_id": "not-uuid"},)
        queries = []
        conn.set_trace_callback(queries.append)
        with pytest.raises(ValueError):
            worker.desired_many_in_snapshot(conn, batch)
        assert queries == [] and memory.memory_id is not None


def test_forged_candidate_content_filtered_before_materialization(setup):
    store, _, room, _, _, index, _ = setup
    memory = save(setup, "规范内容")
    with store.database.connect() as conn:
        _, selected = index._snapshot(conn, (memory.model_copy(update={"content": "伪造内容"}),),
                                      cached_only=True, room_id=room)
    assert selected == () and setup[3].calls == setup[4].calls == []


def test_batch_normalizes_uuid_lookup_like_single_delivery(setup):
    store, *_, worker = setup
    memory = save(setup, "UUID 查询等价")
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        row = dict(currents(conn)[0])
        row["memory_id"] = str(memory.memory_id).upper()
        expected = worker.desired_in_snapshot(conn, row)
        assert expected[0].record is not None
        assert worker.desired_many_in_snapshot(conn, (row,))[row["memory_id"]] == expected


@pytest.mark.parametrize("cached_only", [False, True])
def test_rowwise_control_parity_for_scope_and_mixed_cache_eligibility(setup, cached_only):
    store, service, room, _, _, index, _ = setup
    current = save(setup, "当前讨论候选")
    valid = save(setup, "有效缓存")
    missing = save(setup, "尚未建立缓存", vector=None)
    retired = save(setup, "已撤销候选")
    store.memory.retire(room, retired.memory_id)
    other_room = service.create_room(title="other", idempotency_key=uuid4()).room_id
    other = save(setup, "另一房间", room=other_room)
    correlation = store.get_message(current.source_message_id).message.correlation_id
    memories = (current, other, retired, missing, valid)
    with store.database.connect() as conn:
        _, actual = index._snapshot(conn, memories, cached_only=cached_only, room_id=room, exclude=correlation)
    with store.database.connect() as conn:
        _, expected = rowwise_snapshot(index, conn, memories, cached_only=cached_only, room_id=room, exclude=correlation)
    assert actual == expected
    assert [item[0].memory_id for item in actual] == ([valid.memory_id] if cached_only else [missing.memory_id, valid.memory_id])
