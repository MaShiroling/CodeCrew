from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.orchestration.models import Task, TaskState
from app.storage import (
    SQLiteDatabase,
    StaleTaskRevisionError,
    TaskConflictError,
    TaskNotFoundError,
    TaskRepository,
    TaskRepositoryIntegrityError,
)


def make_repository(tmp_path: Path) -> TaskRepository:
    repository = TaskRepository(SQLiteDatabase(tmp_path / "codecrew.sqlite3"))
    repository.initialize()
    return repository


def test_task_round_trips_across_repository_restart(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(
        issue="persist the workflow",
        repository_path="/tmp/repository",
        metadata={"allowed_paths": ["src"]},
    )
    created = repository.create(task)

    reopened = TaskRepository(SQLiteDatabase(repository.database.path))
    reopened.initialize()
    loaded = reopened.get(task.id)

    assert created == loaded
    assert loaded.revision == 1
    assert loaded.task is not task
    assert reopened.database.schema_version == 7


def test_create_is_idempotent_and_rejects_identity_conflicts(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="same task", repository_path="/tmp/repository")

    first = repository.create(task)
    repeated = repository.create(task.model_copy(deep=True))

    assert repeated == first
    with pytest.raises(TaskConflictError, match="different content"):
        repository.create(task.model_copy(update={"issue": "different"}))
    with pytest.raises(TaskConflictError, match="identity conflicts"):
        repository.create(
            Task(
                trace_id=task.trace_id,
                issue="another task",
                repository_path="/tmp/repository",
            )
        )


def test_save_persists_state_metadata_and_rework_rounds(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="advance me", repository_path="/tmp/repository")
    snapshot = repository.create(task)
    task.transition_to(TaskState.PLANNING)
    task.metadata["owner"] = "orchestrator"
    task.rework_rounds = 1

    saved = repository.save(task, expected_revision=snapshot.revision)
    loaded = repository.get(task.id)

    assert saved == loaded
    assert saved.revision == 2
    assert loaded.task.state is TaskState.PLANNING
    assert loaded.task.rework_rounds == 1
    assert loaded.task.metadata == {"owner": "orchestrator"}
    assert loaded.task.updated_at > snapshot.task.updated_at


def test_stale_writer_cannot_overwrite_newer_task_state(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    original = Task(issue="concurrent task", repository_path="/tmp/repository")
    repository.create(original)
    first_reader = repository.get(original.id)
    stale_reader = repository.get(original.id)
    first_reader.task.transition_to(TaskState.PLANNING)
    repository.save(first_reader.task, expected_revision=first_reader.revision)
    stale_reader.task.transition_to(TaskState.PLANNING)
    stale_reader.task.metadata["stale"] = True

    with pytest.raises(StaleTaskRevisionError, match="expected task revision 1"):
        repository.save(stale_reader.task, expected_revision=stale_reader.revision)

    assert repository.get(original.id).task.metadata == {}


def test_concurrent_saves_allow_exactly_one_revision_winner(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="race", repository_path="/tmp/repository")
    repository.create(task)
    barrier = Barrier(2)

    def update(owner: str) -> str:
        snapshot = repository.get(task.id)
        snapshot.task.metadata["owner"] = owner
        barrier.wait()
        try:
            repository.save(snapshot.task, expected_revision=snapshot.revision)
        except StaleTaskRevisionError:
            return "stale"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(update, ("one", "two")))

    assert sorted(results) == ["saved", "stale"]
    assert repository.get(task.id).revision == 2


def test_immutable_fields_and_missing_tasks_are_rejected(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="immutable", repository_path="/tmp/repository")
    snapshot = repository.create(task)

    with pytest.raises(TaskConflictError, match="immutable.*issue"):
        repository.save(
            task.model_copy(update={"issue": "changed"}),
            expected_revision=snapshot.revision,
        )
    with pytest.raises(TaskNotFoundError):
        repository.get(uuid4())
    with pytest.raises(TaskNotFoundError):
        repository.save(
            Task(issue="missing", repository_path="/tmp/repository"),
            expected_revision=1,
        )


def test_list_filters_state_and_paginates(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    planning = Task(issue="planning", repository_path="/tmp/one")
    planning.transition_to(TaskState.PLANNING)
    created = Task(issue="created", repository_path="/tmp/two")
    repository.create(planning)
    repository.create(created)

    assert [item.task.id for item in repository.list(state=TaskState.PLANNING)] == [
        planning.id
    ]
    assert len(repository.list(limit=1)) == 1
    assert len(repository.list(limit=1, offset=1)) == 1
    with pytest.raises(ValueError, match="positive"):
        repository.list(limit=0)


def test_list_orders_newest_tasks_first_with_stable_pagination(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    older_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    older = Task(issue="older", repository_path="/tmp/one", created_at=older_time,
                 updated_at=older_time)
    newer = Task(issue="newer", repository_path="/tmp/two",
                 created_at=older_time + timedelta(seconds=1),
                 updated_at=older_time + timedelta(seconds=1))
    repository.create(older)
    repository.create(newer)

    assert [item.task.id for item in repository.list()] == [newer.id, older.id]
    assert [item.task.id for item in repository.list(limit=1, offset=0)] == [newer.id]
    assert [item.task.id for item in repository.list(limit=1, offset=1)] == [older.id]


def test_corrupt_indexed_columns_are_not_silently_loaded(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="integrity", repository_path="/tmp/repository")
    repository.create(task)
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            (TaskState.COMPLETED.value, str(task.id)),
        )

    with pytest.raises(TaskRepositoryIntegrityError, match="corrupt"):
        repository.get(task.id)
