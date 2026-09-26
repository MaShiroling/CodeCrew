"""Fail-closed macOS filesystem boundary for a Kimi Code implementer.

This controls writes and blocks reads from the user's real home except for the
managed worktree, private runtime and explicitly supplied CLI resources. It
does not make arbitrary CLI shell execution safe; the restricted agent profile
must omit Bash, and tests run through CodeCrew's separate CommandExecutor.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.workspace.permissions import PermissionPolicy


class KimiBoundaryError(RuntimeError):
    """The requested CLI write boundary cannot be enforced."""


class KimiWriteBoundary:
    def __init__(
        self,
        *,
        worktree: Path,
        runtime_directory: Path,
        policy: PermissionPolicy,
        readable_files: Sequence[Path] = (),
        read_only_files: Sequence[Path] = (),
        worktree_read_only: bool = False,
        sandbox_executable: str = "/usr/bin/sandbox-exec",
    ) -> None:
        self.worktree = self._existing_directory(worktree, "worktree")
        self.runtime_directory = self._existing_directory(runtime_directory, "runtime directory")
        self.policy = policy
        self.worktree_read_only = worktree_read_only
        self.protected_home = Path.home().resolve(strict=True)
        self.readable_files = tuple(
            dict.fromkeys(
                item for path in readable_files for item in self._existing_file_paths(path)
            )
        )
        self.sandbox_executable = sandbox_executable
        self.read_only_files = tuple(
            dict.fromkeys(
                path for file in read_only_files for path in self._existing_file_paths(file)
            )
        )
        if self.runtime_directory.is_relative_to(self.worktree) or self.worktree.is_relative_to(
            self.runtime_directory
        ):
            raise KimiBoundaryError("runtime directory must be separate from the worktree")

        self.allowed_directories = tuple(
            self._allowed_directory(rule) for rule in policy.allowed_paths
        )
        self.denied_paths = tuple(self.worktree / rule for rule in policy.denied_paths)

    @staticmethod
    def _existing_directory(path: Path, label: str) -> Path:
        if not path.is_dir():
            raise KimiBoundaryError(f"{label} must exist and be a directory: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise KimiBoundaryError(f"{label} does not resolve to a directory: {path}")
        return resolved

    @staticmethod
    def _existing_file_paths(path: Path) -> tuple[Path, Path]:
        if not path.is_file():
            raise KimiBoundaryError(f"readable CLI resource must be a file: {path}")
        return path.absolute(), path.resolve(strict=True)

    def _allowed_directory(self, rule: str) -> Path:
        candidate = self.worktree / rule
        if not candidate.is_dir():
            raise KimiBoundaryError(f"allowed write directory does not exist: {candidate}")
        current = self.worktree
        for part in Path(rule).parts:
            current = current / part
            if current.is_symlink():
                raise KimiBoundaryError(f"allowed write directory contains a symlink: {current}")
        if not candidate.resolve(strict=True).is_relative_to(self.worktree):
            raise KimiBoundaryError(f"allowed write directory escapes worktree: {candidate}")
        for denied in self.policy.denied_paths:
            denied_root = self.worktree / denied
            if candidate == denied_root or candidate.is_relative_to(denied_root):
                raise KimiBoundaryError(f"allowed write directory is denied: {candidate}")
        return candidate

    def profile(self) -> str:
        """Build a Seatbelt profile; protect the real home and constrain writes."""
        read_exceptions = [
            *(
                f"(require-not (subpath {json.dumps(str(path))}))"
                for path in (self.worktree, self.runtime_directory)
            ),
            *(
                f"(require-not (literal {json.dumps(str(path))}))"
                for path in (*self.readable_files, *self.read_only_files)
            ),
        ]
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-read-data (require-all "
            f"(subpath {json.dumps(str(self.protected_home))}) " + " ".join(read_exceptions) + "))",
            "(deny file-write*)",
        ]
        write_directories = () if self.worktree_read_only else self.allowed_directories
        for path in (*write_directories, self.runtime_directory):
            lines.append(f"(allow file-write* (subpath {json.dumps(str(path))}))")
        if self.worktree_read_only:
            lines.append(f"(deny file-write* (subpath {json.dumps(str(self.worktree))}))")
        for path in self.denied_paths:
            lines.append(f"(deny file-write* (subpath {json.dumps(str(path))}))")
        for path in self.read_only_files:
            lines.append(f"(deny file-write* (literal {json.dumps(str(path))}))")
        return "\n".join(lines) + "\n"

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """Return a command that cannot silently fall back to unsandboxed CLI."""
        if not argv or any(not arg or "\0" in arg for arg in argv):
            raise KimiBoundaryError("sandboxed command arguments must be nonempty")
        if platform.system() != "Darwin":
            raise KimiBoundaryError("Kimi write boundary requires macOS sandbox-exec")
        executable = Path(self.sandbox_executable)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise KimiBoundaryError("sandbox-exec is unavailable; refusing unsandboxed launch")
        if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
            raise KimiBoundaryError(f"sandboxed executable does not exist: {argv[0]}")
        return [str(executable), "-p", self.profile(), *argv]
