import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents import AgentRole
from app.orchestration.models import Task
from app.storage import (
    AgentRuntimeBinding,
    RuntimeContextConflictError,
    RuntimeContextNotFoundError,
    RuntimeContextRepository,
    SQLiteDatabase,
    StaleRuntimeContextRevisionError,
    TaskRepository,
    WorkflowRuntimeContext,
)
from app.team import MemberRole, WorkflowExecutionError, WorkflowRuntime
from app.verification import (
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
)
from app.workspace import WorktreeHandle


def verification_plan() -> VerificationPlan:
    return VerificationPlan(
        commands=(
            VerificationCommand(
                name="public tests",
                kind=VerificationCheckKind.PUBLIC_TESTS,
                argv=(sys.executable, "-m", "pytest"),
            ),
        )
    )


def make_context(task: Task, tmp_path: Path) -> WorkflowRuntimeContext:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    return WorkflowRuntimeContext(
        task_id=task.id,
        trace_id=task.trace_id,
        room_id=uuid4(),
        worktree=WorktreeHandle(
            task_id=task.id,
            repository_root=tmp_path,
            worktree_path=worktree,
            branch_name="codecrew/runtime-test",
            base_revision="a" * 40,
        ),
        verification_plan=verification_plan(),
        agent_bindings=(
            AgentRuntimeBinding(
                role=AgentRole.PLANNER,
                agent_name="claude-code",
                native_session_id="claude-session-1",
            ),
            AgentRuntimeBinding(
                role=AgentRole.IMPLEMENTER,
                agent_name="codex",
                native_session_id="codex-session-1",
            ),
            AgentRuntimeBinding(
                role=AgentRole.REVIEWER,
                agent_name="claude-reviewer",
            ),
        ),
    )


def make_repositories(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    tasks = TaskRepository(database)
    contexts = RuntimeContextRepository(database)
    tasks.initialize()
    contexts.initialize()
    return tasks, contexts


def test_runtime_context_round_trips_across_restart(tmp_path: Path) -> None:
    tasks, contexts = make_repositories(tmp_path)
    task = Task(issue="recover runtime", repository_path=str(tmp_path))
    tasks.create(task)
    context = make_context(task, tmp_path)
    created = contexts.create(context)

    reopened = RuntimeContextRepository(SQLiteDatabase(contexts.database.path))
    reopened.initialize()
    loaded = reopened.get(task.id)

    assert loaded == created
    assert loaded.context is not context
    assert loaded.revision == 1
    assert reopened.database.schema_version == 8


def test_runtime_context_save_updates_sessions_with_optimistic_lock(
    tmp_path: Path,
) -> None:
    tasks, contexts = make_repositories(tmp_path)
    task = Task(issue="resume agents", repository_path=str(tmp_path))
    tasks.create(task)
    context = make_context(task, tmp_path)
    first = contexts.create(context)
    stale = contexts.get(task.id)
    bindings = tuple(
        binding.model_copy(update={"native_session_id": "codex-session-2"})
        if binding.role is AgentRole.IMPLEMENTER
        else binding
        for binding in first.context.agent_bindings
    )

    saved = contexts.save(
        first.context.model_copy(update={"agent_bindings": bindings}),
        expected_revision=first.revision,
    )

    assert saved.revision == 2
    assert saved.context.updated_at > first.context.updated_at
    assert next(
        binding
        for binding in saved.context.agent_bindings
        if binding.role is AgentRole.IMPLEMENTER
    ).native_session_id == "codex-session-2"
    with pytest.raises(StaleRuntimeContextRevisionError, match="expected.*1"):
        contexts.save(stale.context, expected_revision=stale.revision)


def test_runtime_identity_is_immutable_and_task_must_exist(tmp_path: Path) -> None:
    tasks, contexts = make_repositories(tmp_path)
    task = Task(issue="bound context", repository_path=str(tmp_path))
    tasks.create(task)
    snapshot = contexts.create(make_context(task, tmp_path))

    with pytest.raises(RuntimeContextConflictError, match="immutable.*room_id"):
        contexts.save(
            snapshot.context.model_copy(update={"room_id": uuid4()}),
            expected_revision=snapshot.revision,
        )
    missing = Task(issue="missing", repository_path=str(tmp_path / "missing"))
    with pytest.raises(RuntimeContextConflictError, match="persisted task"):
        contexts.create(make_context(missing, tmp_path / "missing"))
    with pytest.raises(RuntimeContextNotFoundError):
        contexts.get(uuid4())


def test_context_rejects_foreign_worktree_and_duplicate_roles(tmp_path: Path) -> None:
    task = Task(issue="validate context", repository_path=str(tmp_path))
    context = make_context(task, tmp_path)
    payload = context.model_dump()
    payload["worktree"]["task_id"] = uuid4()
    with pytest.raises(ValidationError, match="another task"):
        WorkflowRuntimeContext.model_validate(payload)
    with pytest.raises(ValidationError, match="roles must be unique"):
        WorkflowRuntimeContext(
            **context.model_dump(exclude={"agent_bindings"}),
            agent_bindings=(context.agent_bindings[0], context.agent_bindings[0]),
        )


def test_workflow_runtime_converts_to_and_from_persisted_context(
    tmp_path: Path,
) -> None:
    task = Task(issue="rebuild runtime", repository_path=str(tmp_path))
    context = make_context(task, tmp_path)

    runtime = WorkflowRuntime.from_context(task, context)
    rebuilt = runtime.to_context()

    assert runtime.room_id == context.room_id
    assert runtime.agent_names[MemberRole.IMPLEMENTER] == "codex"
    assert runtime.native_session_ids[MemberRole.PLANNER] == "claude-session-1"
    assert rebuilt.model_copy(update={"updated_at": context.updated_at}) == context
    with pytest.raises(WorkflowExecutionError, match="another task or trace"):
        WorkflowRuntime.from_context(
            Task(issue="foreign", repository_path=str(tmp_path)), context
        )
