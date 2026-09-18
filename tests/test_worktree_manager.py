import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.workspace import (
    InvalidRepositoryError,
    InvalidRevisionError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeManager,
    WorktreeNotFoundError,
)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "README.md").write_text("original\n")
    git(path, "add", "README.md")
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


@pytest.mark.asyncio
async def test_creates_isolated_worktree_from_resolved_baseline(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    manager = WorktreeManager(tmp_path / "managed")
    task_id = uuid4()

    handle = await manager.create(task_id=task_id, repository=repository)
    (handle.worktree_path / "README.md").write_text("changed in worktree\n")

    assert handle.task_id == task_id
    assert handle.repository_root == repository
    assert handle.base_revision == git(repository, "rev-parse", "HEAD")
    assert handle.branch_name == f"codecrew/{task_id}"
    assert git(handle.worktree_path, "branch", "--show-current") == handle.branch_name
    assert (repository / "README.md").read_text() == "original\n"


@pytest.mark.asyncio
async def test_create_is_idempotent_and_survives_manager_restart(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    root = tmp_path / "managed"
    task_id = uuid4()
    first = await WorktreeManager(root).create(task_id=task_id, repository=repository)

    restarted = WorktreeManager(root)
    inspected = await restarted.inspect(task_id)
    repeated = await restarted.create(task_id=task_id, repository=repository)

    assert inspected == first
    assert repeated == first


@pytest.mark.asyncio
async def test_different_tasks_have_distinct_branches_and_paths(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    manager = WorktreeManager(tmp_path / "managed")

    first = await manager.create(task_id=uuid4(), repository=repository)
    second = await manager.create(task_id=uuid4(), repository=repository)

    assert first.worktree_path != second.worktree_path
    assert first.branch_name != second.branch_name


@pytest.mark.asyncio
async def test_accepts_repository_subdirectory_but_records_root(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    subdirectory = repository / "src"
    subdirectory.mkdir()

    handle = await WorktreeManager(tmp_path / "managed").create(
        task_id=uuid4(), repository=subdirectory
    )

    assert handle.repository_root == repository


@pytest.mark.asyncio
async def test_rejects_invalid_repository_and_revision(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path / "managed")
    ordinary_directory = tmp_path / "ordinary"
    ordinary_directory.mkdir()

    with pytest.raises(InvalidRepositoryError, match="not a Git working tree"):
        await manager.create(task_id=uuid4(), repository=ordinary_directory)

    repository = make_repository(tmp_path / "repository")
    with pytest.raises(InvalidRevisionError, match="invalid base revision"):
        await manager.create(
            task_id=uuid4(), repository=repository, base_revision="does-not-exist"
        )


@pytest.mark.asyncio
async def test_task_cannot_be_reused_for_another_repository(tmp_path: Path) -> None:
    first_repository = make_repository(tmp_path / "first")
    second_repository = make_repository(tmp_path / "second")
    manager = WorktreeManager(tmp_path / "managed")
    task_id = uuid4()
    await manager.create(task_id=task_id, repository=first_repository)

    with pytest.raises(WorktreeConflictError, match="another repository"):
        await manager.create(task_id=task_id, repository=second_repository)


@pytest.mark.asyncio
async def test_managed_root_must_be_outside_target_repository(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    manager = WorktreeManager(repository / ".codecrew" / "worktrees")

    with pytest.raises(WorktreeConflictError, match="outside the target repository"):
        await manager.create(task_id=uuid4(), repository=repository)

    assert git(repository, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_remove_refuses_dirty_worktree_unless_forced(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    root = tmp_path / "managed"
    manager = WorktreeManager(root)
    task_id = uuid4()
    handle = await manager.create(task_id=task_id, repository=repository)
    (handle.worktree_path / "new-file.txt").write_text("uncommitted")

    with pytest.raises(WorktreeDirtyError, match="uncommitted changes"):
        await manager.remove(task_id)
    assert handle.worktree_path.exists()

    await manager.remove(task_id, force=True)

    assert not handle.worktree_path.exists()
    assert not (root / f"{task_id}.json").exists()
    assert handle.branch_name not in git(repository, "branch", "--format=%(refname:short)")


@pytest.mark.asyncio
async def test_clean_remove_can_keep_branch(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    manager = WorktreeManager(tmp_path / "managed")
    task_id = uuid4()
    handle = await manager.create(task_id=task_id, repository=repository)

    await manager.remove(task_id, delete_branch=False)

    assert not handle.worktree_path.exists()
    assert handle.branch_name in git(repository, "branch", "--format=%(refname:short)")


@pytest.mark.asyncio
async def test_original_repository_dirty_state_is_untouched(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    (repository / "README.md").write_text("user's uncommitted work\n")

    handle = await WorktreeManager(tmp_path / "managed").create(
        task_id=uuid4(), repository=repository
    )

    assert (repository / "README.md").read_text() == "user's uncommitted work\n"
    assert (handle.worktree_path / "README.md").read_text() == "original\n"
    assert git(repository, "status", "--porcelain") == "M README.md"


@pytest.mark.asyncio
async def test_unknown_and_missing_managed_worktree_are_reported(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repository")
    manager = WorktreeManager(tmp_path / "managed")

    with pytest.raises(WorktreeNotFoundError, match="not found"):
        await manager.inspect(UUID(int=0))

    task_id = uuid4()
    handle = await manager.create(task_id=task_id, repository=repository)
    git(repository, "worktree", "remove", "--force", str(handle.worktree_path))

    with pytest.raises(WorktreeConflictError, match="is missing"):
        await manager.inspect(task_id)


def test_manager_configuration_must_be_valid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        WorktreeManager(tmp_path, git_executable=" ")
    with pytest.raises(ValueError, match="must be positive"):
        WorktreeManager(tmp_path, command_timeout_seconds=0)
