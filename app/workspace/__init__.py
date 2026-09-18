"""Worktree and permission isolation (milestone four)."""

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
    "GitCommandError",
    "InvalidRepositoryError",
    "InvalidRevisionError",
    "WorktreeConflictError",
    "WorktreeDirtyError",
    "WorktreeError",
    "WorktreeHandle",
    "WorktreeManager",
    "WorktreeNotFoundError",
]
