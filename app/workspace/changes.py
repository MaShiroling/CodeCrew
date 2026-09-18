import asyncio
import os
import tempfile
from enum import Enum
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.orchestration.models import utc_now
from app.storage import ArtifactReference, ArtifactStore, ArtifactType
from app.workspace.worktrees import WorktreeHandle


class WorkspaceChangeError(RuntimeError):
    """Raised when a task worktree cannot be inspected consistently."""


class ChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNMERGED = "unmerged"


class ChangedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    kind: ChangeKind
    old_path: str | None = Field(default=None, min_length=1)
    similarity_percent: int | None = Field(default=None, ge=0, le=100)

    @field_validator("path", "old_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise ValueError("changed file path must be repository-relative")
        return value


class WorkspaceChangeSet(BaseModel):
    """Immutable evidence describing all changes since the task baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    base_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    head_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    changed_files: tuple[ChangedFile, ...] = ()
    has_effective_diff: bool
    diff_artifact: ArtifactReference | None = None
    manifest_artifact: ArtifactReference
    collected_at: AwareDatetime = Field(default_factory=utc_now)


class WorkspaceChangeCollector:
    """Collect a binary-safe baseline diff without mutating the worktree index."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        git_executable: str = "git",
        command_timeout_seconds: float = 30,
    ) -> None:
        if not git_executable.strip():
            raise ValueError("git_executable must not be empty")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.artifacts = artifacts
        self.git_executable = git_executable
        self.command_timeout_seconds = command_timeout_seconds

    async def collect(
        self,
        handle: WorktreeHandle,
        *,
        trace_id: UUID,
        created_by: str = "implementer",
    ) -> WorkspaceChangeSet:
        worktree = handle.worktree_path.resolve()
        if not worktree.is_dir():
            raise WorkspaceChangeError(f"worktree directory does not exist: {worktree}")
        actual_root = Path(
            (await self._git(worktree, "rev-parse", "--show-toplevel")).decode().strip()
        ).resolve()
        if actual_root != worktree:
            raise WorkspaceChangeError(f"path is not the expected worktree root: {worktree}")

        head_revision = (
            await self._git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
        ).decode().strip()
        untracked = self._split_paths(
            await self._git(
                worktree,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            )
        )
        added_since_base = self._split_paths(
            await self._git(
                worktree,
                "diff",
                "--name-only",
                "--diff-filter=A",
                "-z",
                handle.base_revision,
                "--",
            )
        )
        added_paths = tuple(
            sorted(
                {
                    path
                    for path in (*untracked, *added_since_base)
                    if os.path.lexists(worktree / path)
                }
            )
        )
        temporary_index = self._temporary_index_path(worktree)
        environment = {"GIT_INDEX_FILE": str(temporary_index)}
        try:
            await self._git(
                worktree,
                "read-tree",
                handle.base_revision,
                env=environment,
            )
            if added_paths:
                await self._git(worktree, "add", "-N", "--", *added_paths, env=environment)
            name_status = await self._git(
                worktree,
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                handle.base_revision,
                "--",
                env=environment,
            )
            patch = await self._git(
                worktree,
                "diff",
                "--binary",
                "--no-ext-diff",
                "--find-renames",
                "--full-index",
                handle.base_revision,
                "--",
                env=environment,
            )
        finally:
            temporary_index.unlink(missing_ok=True)

        changed_files = self._parse_name_status(name_status)
        has_effective_diff = bool(changed_files)
        diff_reference: ArtifactReference | None = None
        if has_effective_diff:
            diff_metadata = self.artifacts.put_bytes(
                patch,
                task_id=handle.task_id,
                trace_id=trace_id,
                type=ArtifactType.DIFF,
                media_type="text/x-diff",
                created_by=created_by,
                filename="workspace.patch",
                metadata={"base_revision": handle.base_revision},
            )
            diff_reference = ArtifactReference.from_metadata(
                diff_metadata,
                summary=f"Git patch containing {len(changed_files)} changed files",
            )

        manifest = {
            "task_id": str(handle.task_id),
            "trace_id": str(trace_id),
            "base_revision": handle.base_revision,
            "head_revision": head_revision,
            "has_effective_diff": has_effective_diff,
            "changed_files": [file.model_dump(mode="json") for file in changed_files],
            "diff_artifact_id": (
                str(diff_reference.artifact_id) if diff_reference is not None else None
            ),
        }
        manifest_metadata = self.artifacts.put_json(
            manifest,
            task_id=handle.task_id,
            trace_id=trace_id,
            type=ArtifactType.CHANGESET,
            created_by=created_by,
            filename="changeset.json",
            metadata={"base_revision": handle.base_revision},
        )
        manifest_reference = ArtifactReference.from_metadata(
            manifest_metadata,
            summary=f"Workspace change manifest containing {len(changed_files)} files",
        )
        return WorkspaceChangeSet(
            task_id=handle.task_id,
            trace_id=trace_id,
            base_revision=handle.base_revision,
            head_revision=head_revision,
            changed_files=changed_files,
            has_effective_diff=has_effective_diff,
            diff_artifact=diff_reference,
            manifest_artifact=manifest_reference,
        )

    async def _git(
        self,
        cwd: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> bytes:
        process_environment = None if env is None else {**os.environ, **env}
        try:
            process = await asyncio.create_subprocess_exec(
                self.git_executable,
                *arguments,
                cwd=cwd,
                env=process_environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise WorkspaceChangeError(f"failed to start Git: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WorkspaceChangeError("Git change collection timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise WorkspaceChangeError(
                f"git {' '.join(arguments)} exited with {process.returncode}: {detail}"
            )
        return stdout

    @staticmethod
    def _temporary_index_path(worktree: Path) -> Path:
        descriptor, name = tempfile.mkstemp(prefix="codecrew-index-", dir=worktree.parent)
        os.close(descriptor)
        path = Path(name)
        path.unlink()
        return path

    @staticmethod
    def _split_paths(output: bytes) -> tuple[str, ...]:
        return tuple(
            item.decode("utf-8", errors="surrogateescape")
            for item in output.split(b"\0")
            if item
        )

    @classmethod
    def _parse_name_status(cls, output: bytes) -> tuple[ChangedFile, ...]:
        fields = list(cls._split_paths(output))
        changes: list[ChangedFile] = []
        index = 0
        status_map = {
            "A": ChangeKind.ADDED,
            "M": ChangeKind.MODIFIED,
            "D": ChangeKind.DELETED,
            "T": ChangeKind.TYPE_CHANGED,
            "U": ChangeKind.UNMERGED,
        }
        while index < len(fields):
            status = fields[index]
            index += 1
            code = status[0]
            if code in {"R", "C"}:
                if index + 1 >= len(fields):
                    raise WorkspaceChangeError("malformed Git rename/copy status output")
                old_path, path = fields[index], fields[index + 1]
                index += 2
                similarity = int(status[1:]) if status[1:].isdigit() else None
                changes.append(
                    ChangedFile(
                        path=path,
                        old_path=old_path,
                        kind=ChangeKind.RENAMED if code == "R" else ChangeKind.COPIED,
                        similarity_percent=similarity,
                    )
                )
                continue
            if code not in status_map or index >= len(fields):
                raise WorkspaceChangeError(f"unsupported Git status output: {status!r}")
            changes.append(ChangedFile(path=fields[index], kind=status_map[code]))
            index += 1
        return tuple(sorted(changes, key=lambda change: change.path))
