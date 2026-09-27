from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage import SQLiteDatabase
from app.trace import (
    TraceActorKind,
    TraceEvent,
    TraceEventNotFoundError,
    TraceEventType,
    TraceIdempotencyConflictError,
    TraceStore,
)


def make_store(tmp_path: Path) -> TraceStore:
    store = TraceStore(SQLiteDatabase(tmp_path / "codecrew.sqlite3"))
    store.initialize()
    return store


def event(*, task_id=None, trace_id=None, key="event-1", **updates) -> TraceEvent:
    values = {
        "task_id": task_id or uuid4(),
        "trace_id": trace_id or uuid4(),
        "type": TraceEventType.WORKFLOW_DECISION,
        "actor_kind": TraceActorKind.DETERMINISTIC,
        "actor_id": "workflow_controller",
        "payload": {"state_before": "created", "state_after": "planning"},
        "idempotency_key": key,
    }
    values.update(updates)
    return TraceEvent(**values)


def test_trace_round_trips_across_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = event()
    stored = store.append(original)

    reopened = TraceStore(SQLiteDatabase(store.database.path))
    reopened.initialize()

    assert reopened.get(original.event_id) == stored
    assert reopened.database.schema_version == 9
    with pytest.raises(TraceEventNotFoundError):
        reopened.get(uuid4())


def test_caller_owned_trace_append_rolls_back_with_transaction(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = event()
    with pytest.raises(RuntimeError, match="rollback"), store.database.transaction() as connection:
        store.append_in_transaction(connection, original)
        raise RuntimeError("rollback")
    assert not store.list(trace_id=original.trace_id)
    with store.database.transaction() as connection:
        first = store.append_in_transaction(connection, original)
        assert store.append_in_transaction(connection, original) == first
    assert store.get(original.event_id) == first


def test_caller_owned_trace_append_rejects_autocommit_connection(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with store.database.connect() as connection, pytest.raises(ValueError, match="active transaction"):
        store.append_in_transaction(connection, event())


def test_trace_append_is_idempotent_and_rejects_conflicts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = event()
    first = store.append(original)
    repeated = store.append(original.model_copy(update={"event_id": uuid4()}))

    assert repeated == first
    with pytest.raises(TraceIdempotencyConflictError, match="different event"):
        store.append(
            original.model_copy(
                update={"event_id": uuid4(), "payload": {"state_after": "failed"}}
            )
        )


def test_trace_filters_and_cursor_reads_are_deterministic(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task_id = uuid4()
    trace_id = uuid4()
    first = store.append(event(task_id=task_id, trace_id=trace_id, key="first"))
    second = store.append(
        event(
            task_id=task_id,
            trace_id=trace_id,
            key="second",
            type=TraceEventType.AGENT_TURN_COMPLETED,
        )
    )
    store.append(event(key="foreign"))

    assert store.list(trace_id=trace_id) == (first, second)
    assert store.list(task_id=task_id, after_sequence=first.sequence) == (second,)
    assert store.list(
        trace_id=trace_id, type=TraceEventType.AGENT_TURN_COMPLETED
    ) == (second,)
    with pytest.raises(ValueError, match="required"):
        store.list()


def test_concurrent_trace_writers_preserve_sequence(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task_id = uuid4()
    trace_id = uuid4()

    def append(index: int):
        return store.append(
            event(task_id=task_id, trace_id=trace_id, key=f"concurrent-{index}")
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        written = tuple(executor.map(append, range(20)))

    persisted = store.list(trace_id=trace_id)
    assert len(written) == len(persisted) == 20
    assert [item.sequence for item in persisted] == sorted(
        item.sequence for item in persisted
    )
    assert len({item.sequence for item in persisted}) == 20
