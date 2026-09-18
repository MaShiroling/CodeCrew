import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.workspace import (
    ChangeKind,
    WorkspaceChangeCollector,
    WorkspaceChangeError,
    WorktreeManager,
)


def git(repository: Path, *arguments: str, input: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        input=input,
    )
    return result.stdout.decode().strip()


def make_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "keep.txt").write_text("keep\n")
    (path / "modify.txt").write_text("before\n")
    (path / "delete.txt").write_text("delete\n")
    (path / "rename-old.txt").write_text("rename me\n")
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


async def make_collector(tmp_path: Path):
    repository = make_repository(tmp_path / "repository")
    handle = await WorktreeManager(tmp_path / "worktrees").create(
        task_id=uuid4(), repository=repository
    )
    store = ArtifactStore(
        SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts"
    )
    store.initialize()
    return repository, handle, store, WorkspaceChangeCollector(store)


@pytest.mark.asyncio
async def test_clean_worktree_produces_manifest_without_patch(tmp_path: Path) -> None:
    _, handle, store, collector = await make_collector(tmp_path)
    trace_id = uuid4()

    changes = await collector.collect(handle, trace_id=trace_id)

    assert not changes.has_effective_diff
    assert changes.changed_files == ()
    assert changes.diff_artifact is None
    assert changes.manifest_artifact.type is ArtifactType.CHANGESET
    manifest = store.read_json(changes.manifest_artifact.artifact_id)
    assert manifest["has_effective_diff"] is False
    assert manifest["changed_files"] == []


@pytest.mark.asyncio
async def test_collects_modified_deleted_renamed_and_untracked_files(tmp_path: Path) -> None:
    _, handle, store, collector = await make_collector(tmp_path)
    worktree = handle.worktree_path
    (worktree / "modify.txt").write_text("after\n")
    (worktree / "delete.txt").unlink()
    (worktree / "rename-old.txt").rename(worktree / "rename-new.txt")
    (worktree / "added empty.txt").touch()
    (worktree / "binary.bin").write_bytes(b"\x00\xff\x01")
    git(worktree, "add", "modify.txt")
    staged_before = git(worktree, "diff", "--cached", "--name-only")

    changes = await collector.collect(handle, trace_id=uuid4())

    by_path = {item.path: item for item in changes.changed_files}
    assert by_path["modify.txt"].kind is ChangeKind.MODIFIED
    assert by_path["delete.txt"].kind is ChangeKind.DELETED
    assert by_path["rename-new.txt"].kind is ChangeKind.RENAMED
    assert by_path["rename-new.txt"].old_path == "rename-old.txt"
    assert by_path["added empty.txt"].kind is ChangeKind.ADDED
    assert by_path["binary.bin"].kind is ChangeKind.ADDED
    assert changes.has_effective_diff
    assert changes.diff_artifact is not None
    assert changes.diff_artifact.type is ArtifactType.DIFF
    patch = store.read_bytes(changes.diff_artifact.artifact_id)
    assert b"diff --git" in patch
    assert b"added empty.txt" in patch
    assert b"GIT binary patch" in patch
    assert git(worktree, "diff", "--cached", "--name-only") == staged_before


@pytest.mark.asyncio
async def test_patch_recreates_changes_on_clean_checkout(tmp_path: Path) -> None:
    repository, handle, store, collector = await make_collector(tmp_path)
    worktree = handle.worktree_path
    (worktree / "modify.txt").write_text("patched\n")
    (worktree / "new.txt").write_text("new\n")
    changes = await collector.collect(handle, trace_id=uuid4())
    assert changes.diff_artifact is not None
    patch = store.read_bytes(changes.diff_artifact.artifact_id)

    target = tmp_path / "target"
    git(tmp_path, "clone", str(repository), str(target))
    git(target, "apply", "--binary", "-", input=patch)

    assert (target / "modify.txt").read_text() == "patched\n"
    assert (target / "new.txt").read_text() == "new\n"


@pytest.mark.asyncio
async def test_changes_committed_by_agent_remain_visible_from_baseline(tmp_path: Path) -> None:
    _, handle, _, collector = await make_collector(tmp_path)
    (handle.worktree_path / "modify.txt").write_text("committed\n")
    git(handle.worktree_path, "add", "modify.txt")
    git(
        handle.worktree_path,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "agent change",
    )

    changes = await collector.collect(handle, trace_id=uuid4())

    assert changes.head_revision != changes.base_revision
    assert changes.changed_files[0].path == "modify.txt"
    assert changes.changed_files[0].kind is ChangeKind.MODIFIED


@pytest.mark.asyncio
async def test_ignored_files_are_not_collected(tmp_path: Path) -> None:
    _, handle, _, collector = await make_collector(tmp_path)
    (handle.worktree_path / ".gitignore").write_text("secret.env\n")
    git(handle.worktree_path, "add", ".gitignore")
    (handle.worktree_path / "secret.env").write_text("token=secret")

    changes = await collector.collect(handle, trace_id=uuid4())

    assert [item.path for item in changes.changed_files] == [".gitignore"]


@pytest.mark.asyncio
async def test_missing_or_nested_worktree_is_rejected(tmp_path: Path) -> None:
    _, handle, _, collector = await make_collector(tmp_path)
    missing = handle.model_copy(update={"worktree_path": tmp_path / "missing"})
    with pytest.raises(WorkspaceChangeError, match="does not exist"):
        await collector.collect(missing, trace_id=uuid4())

    nested_path = handle.worktree_path / "nested"
    nested_path.mkdir()
    nested = handle.model_copy(update={"worktree_path": nested_path})
    with pytest.raises(WorkspaceChangeError, match="not the expected worktree root"):
        await collector.collect(nested, trace_id=uuid4())


def test_collector_configuration_must_be_valid(tmp_path: Path) -> None:
    store = ArtifactStore(SQLiteDatabase(tmp_path / "db.sqlite3"), tmp_path / "artifacts")
    with pytest.raises(ValueError, match="must not be empty"):
        WorkspaceChangeCollector(store, git_executable=" ")
    with pytest.raises(ValueError, match="must be positive"):
        WorkspaceChangeCollector(store, command_timeout_seconds=0)
