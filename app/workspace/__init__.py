"""Worktree and permission isolation (milestone four)."""

from app.workspace.changes import (
    ChangedFile,
    ChangeKind,
    WorkspaceChangeCollector,
    WorkspaceChangeError,
    WorkspaceChangeSet,
)
from app.workspace.worktrees import (
    GitCommandError,
    InvalidRepositoryError,
    InvalidRevisionError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeError,
    WorktreeHandle,
    WorktreeManager,
    WorktreeNotFoundError,
)

__all__ = [
    "ChangeKind",
    "ChangedFile",
    "GitCommandError",
    "InvalidRepositoryError",
    "InvalidRevisionError",
    "WorkspaceChangeCollector",
    "WorkspaceChangeError",
    "WorkspaceChangeSet",
    "WorktreeConflictError",
    "WorktreeDirtyError",
    "WorktreeError",
    "WorktreeHandle",
    "WorktreeManager",
    "WorktreeNotFoundError",
]
