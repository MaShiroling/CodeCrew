import asyncio
import os
import tempfile
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.orchestration.models import utc_now


class WorktreeError(RuntimeError):
    """Base error for isolated Git workspace management."""


class GitCommandError(WorktreeError):
    pass


class InvalidRepositoryError(WorktreeError):
    pass


class InvalidRevisionError(WorktreeError):
    pass


class WorktreeNotFoundError(WorktreeError):
    pass


class WorktreeConflictError(WorktreeError):
    pass


class WorktreeDirtyError(WorktreeError):
    pass


class WorktreeHandle(BaseModel):
    """Persistent identity and baseline for one task-owned Git worktree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    repository_root: Path
    worktree_path: Path
    branch_name: str = Field(min_length=1)
    base_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    created_at: AwareDatetime = Field(default_factory=utc_now)


class WorktreeManager:
    """Create and recover task-owned worktrees without touching the main checkout."""

    def __init__(
        self,
        root: Path,
        *,
        git_executable: str = "git",
        command_timeout_seconds: float = 30,
    ) -> None:
        if not git_executable.strip():
            raise ValueError("git_executable must not be empty")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.root = root.resolve()
        self.git_executable = git_executable
        self.command_timeout_seconds = command_timeout_seconds
        self._operation_lock = asyncio.Lock()

    async def create(
        self,
        *,
        task_id: UUID,
        repository: Path,
        base_revision: str = "HEAD",
    ) -> WorktreeHandle:
        if not base_revision.strip():
            raise ValueError("base_revision must not be empty")
        async with self._operation_lock:
            repository_root = await self._repository_root(repository)
            if self.root == repository_root or self.root.is_relative_to(repository_root):
                raise WorktreeConflictError(
                    "managed worktree root must be outside the target repository"
                )
            manifest_path = self._manifest_path(task_id)
            if manifest_path.exists():
                existing = await self._inspect_unlocked(task_id)
                if existing.repository_root != repository_root:
                    raise WorktreeConflictError(
                        f"task {task_id} already belongs to another repository"
                    )
                return existing

            base_commit = await self._resolve_revision(repository_root, base_revision)
            worktree_path = self._worktree_path(task_id)
            branch_name = self._branch_name(task_id)
            if worktree_path.exists():
                raise WorktreeConflictError(
                    f"managed worktree path already exists: {worktree_path}"
                )

            self.root.mkdir(parents=True, exist_ok=True)
            try:
                await self._git(
                    repository_root,
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    base_commit,
                )
            except GitCommandError as exc:
                raise WorktreeConflictError(f"failed to create worktree: {exc}") from exc

            handle = WorktreeHandle(
                task_id=task_id,
                repository_root=repository_root,
                worktree_path=worktree_path,
                branch_name=branch_name,
                base_revision=base_commit,
            )
            try:
                self._write_manifest(handle)
            except BaseException:
                await self._git(
                    repository_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                )
                await self._delete_branch(repository_root, branch_name)
                raise
            return handle

    async def inspect(self, task_id: UUID) -> WorktreeHandle:
        async with self._operation_lock:
            return await self._inspect_unlocked(task_id)

    async def remove(
        self,
        task_id: UUID,
        *,
        delete_branch: bool = True,
        force: bool = False,
    ) -> None:
        async with self._operation_lock:
            handle = await self._inspect_unlocked(task_id)
            if not force and await self._is_dirty(handle.worktree_path):
                raise WorktreeDirtyError(
                    f"worktree has uncommitted changes: {handle.worktree_path}"
                )

            arguments = ["worktree", "remove"]
            if force:
                arguments.append("--force")
            arguments.append(str(handle.worktree_path))
            await self._git(handle.repository_root, *arguments)
            if delete_branch:
                await self._delete_branch(handle.repository_root, handle.branch_name)
            self._manifest_path(task_id).unlink(missing_ok=True)

    async def _inspect_unlocked(self, task_id: UUID) -> WorktreeHandle:
        manifest_path = self._manifest_path(task_id)
        try:
            handle = WorktreeHandle.model_validate_json(manifest_path.read_text())
        except FileNotFoundError as exc:
            raise WorktreeNotFoundError(f"worktree not found for task: {task_id}") from exc
        except (OSError, ValueError) as exc:
            raise WorktreeConflictError(f"invalid worktree manifest: {manifest_path}") from exc

        expected_path = self._worktree_path(task_id)
        if handle.task_id != task_id or handle.worktree_path.resolve() != expected_path:
            raise WorktreeConflictError(f"worktree manifest escaped managed root: {manifest_path}")
        if not handle.worktree_path.is_dir():
            raise WorktreeConflictError(f"managed worktree is missing: {handle.worktree_path}")

        actual_root = await self._repository_root(handle.worktree_path)
        if actual_root != handle.worktree_path.resolve():
            raise WorktreeConflictError(
                f"managed path is not a worktree root: {handle.worktree_path}"
            )
        actual_branch = await self._git(handle.worktree_path, "branch", "--show-current")
        if actual_branch.strip() != handle.branch_name:
            raise WorktreeConflictError(
                f"managed worktree branch changed from {handle.branch_name!r}"
            )
        return handle

    async def _repository_root(self, repository: Path) -> Path:
        candidate = repository.resolve()
        if not candidate.is_dir():
            raise InvalidRepositoryError(f"repository directory does not exist: {candidate}")
        try:
            inside = await self._git(candidate, "rev-parse", "--is-inside-work-tree")
            root = await self._git(candidate, "rev-parse", "--show-toplevel")
        except GitCommandError as exc:
            raise InvalidRepositoryError(f"not a Git working tree: {candidate}") from exc
        if inside.strip() != "true":
            raise InvalidRepositoryError(f"not a Git working tree: {candidate}")
        return Path(root.strip()).resolve()

    async def _resolve_revision(self, repository: Path, revision: str) -> str:
        try:
            resolved = await self._git(
                repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            )
        except GitCommandError as exc:
            raise InvalidRevisionError(f"invalid base revision: {revision!r}") from exc
        return resolved.strip()

    async def _is_dirty(self, worktree: Path) -> bool:
        output = await self._git(worktree, "status", "--porcelain", "--untracked-files=all")
        return bool(output.strip())

    async def _delete_branch(self, repository: Path, branch_name: str) -> None:
        await self._git(repository, "branch", "-D", branch_name)

    async def _git(self, cwd: Path, *arguments: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.git_executable,
                *arguments,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise GitCommandError(f"failed to start Git: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GitCommandError("Git command timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise GitCommandError(
                f"git {' '.join(arguments)} exited with {process.returncode}: {detail}"
            )
        return stdout.decode(errors="replace")

    def _write_manifest(self, handle: WorktreeHandle) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix="worktree-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(handle.model_dump_json(indent=2))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._manifest_path(handle.task_id))
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _manifest_path(self, task_id: UUID) -> Path:
        return self.root / f"{task_id}.json"

    def _worktree_path(self, task_id: UUID) -> Path:
        path = (self.root / str(task_id)).resolve()
        if path.parent != self.root:
            raise WorktreeConflictError("task worktree path escaped managed root")
        return path

    @staticmethod
    def _branch_name(task_id: UUID) -> str:
        return f"codecrew/{task_id}"
