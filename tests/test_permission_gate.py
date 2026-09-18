import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.workspace import (
    PathRole,
    PermissionGate,
    PermissionPolicy,
    PermissionViolationKind,
    WorkspaceChangeCollector,
    WorkspacePermissionError,
    WorktreeManager,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True
    )


def make_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    for filename, content in (
        ("src/app.py", "value = 1\n"),
        ("tests/test_app.py", "def test_value(): pass\n"),
        ("docs/guide.md", "guide\n"),
        ("protected/secret.txt", "secret\n"),
    ):
        target = path / filename
        target.parent.mkdir(exist_ok=True)
        target.write_text(content)
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "initial",
    )
    return path.resolve()


async def make_context(tmp_path: Path, policy: PermissionPolicy):
    repository = make_repository(tmp_path / "repository")
    handle = await WorktreeManager(tmp_path / "worktrees").create(
        task_id=uuid4(), repository=repository
    )
    store = ArtifactStore(
        SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts"
    )
    store.initialize()
    collector = WorkspaceChangeCollector(store)
    return handle, store, collector, PermissionGate(store, policy)


@pytest.mark.asyncio
async def test_allowed_changes_pass_and_report_is_persisted(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src", "tests"))
    handle, store, collector, gate = await make_context(tmp_path, policy)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    (handle.worktree_path / "tests/new_test.py").write_text("def test_new(): pass\n")
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    assert report.passed
    assert report.violations == ()
    assert set(report.checked_paths) == {"src/app.py", "tests/new_test.py"}
    assert report.artifact.type is ArtifactType.PERMISSION_REPORT
    persisted = store.read_json(report.artifact.artifact_id)
    assert persisted["passed"] is True


@pytest.mark.asyncio
async def test_outside_allowed_and_denied_paths_are_reported(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=(".",), denied_paths=("protected", ".env"))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    (handle.worktree_path / "docs/guide.md").write_text("allowed globally\n")
    (handle.worktree_path / "protected/secret.txt").write_text("changed\n")
    (handle.worktree_path / ".env").write_text("TOKEN=secret\n")
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    assert not report.passed
    by_path = {item.path: item for item in report.violations}
    assert by_path["protected/secret.txt"].kind is PermissionViolationKind.DENIED_PATH
    assert by_path[".env"].kind is PermissionViolationKind.DENIED_PATH
    assert "docs/guide.md" not in by_path


@pytest.mark.asyncio
async def test_path_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src",))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    (handle.worktree_path / "docs/guide.md").write_text("changed\n")
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    assert report.violations[0].kind is PermissionViolationKind.OUTSIDE_ALLOWED_PATHS
    assert report.violations[0].path == "docs/guide.md"


@pytest.mark.asyncio
async def test_rename_checks_both_source_and_destination(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src",))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    (handle.worktree_path / "docs/guide.md").rename(
        handle.worktree_path / "src/guide.md"
    )
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    assert any(
        item.path == "docs/guide.md"
        and item.path_role is PathRole.PREVIOUS
        and item.kind is PermissionViolationKind.OUTSIDE_ALLOWED_PATHS
        for item in report.violations
    )


@pytest.mark.asyncio
async def test_symlink_outside_worktree_is_rejected(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src",))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.symlink(outside, handle.worktree_path / "src/escape.py")
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    violation = next(
        item
        for item in report.violations
        if item.kind is PermissionViolationKind.SYMLINK_ESCAPE
    )
    assert violation.path == "src/escape.py"
    assert violation.resolved_path == str(outside)


@pytest.mark.asyncio
async def test_symlink_into_denied_repository_path_is_rejected(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src",), denied_paths=("protected",))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    os.symlink("../protected/secret.txt", handle.worktree_path / "src/secret-link")
    changes = await collector.collect(handle, trace_id=uuid4())

    report = gate.check(handle, changes)

    violation = next(
        item
        for item in report.violations
        if item.kind is PermissionViolationKind.SYMLINK_ESCAPE
    )
    assert violation.rule == "protected"
    assert violation.resolved_path == "protected/secret.txt"


@pytest.mark.asyncio
async def test_change_set_must_match_worktree_task_and_baseline(tmp_path: Path) -> None:
    policy = PermissionPolicy(allowed_paths=("src",))
    handle, _, collector, gate = await make_context(tmp_path, policy)
    changes = await collector.collect(handle, trace_id=uuid4())

    with pytest.raises(WorkspacePermissionError, match="another task"):
        gate.check(handle, changes.model_copy(update={"task_id": uuid4()}))
    with pytest.raises(WorkspacePermissionError, match="baseline"):
        gate.check(handle, changes.model_copy(update={"base_revision": "f" * 40}))


@pytest.mark.parametrize("rule", ["/absolute", "../escape", "a/../b", r"src\\windows"])
def test_policy_rejects_unsafe_rules(rule: str) -> None:
    with pytest.raises(ValidationError, match="repository-relative|forward slashes"):
        PermissionPolicy(allowed_paths=(rule,))


def test_policy_requires_at_least_one_allowed_path() -> None:
    with pytest.raises(ValidationError):
        PermissionPolicy(allowed_paths=())
