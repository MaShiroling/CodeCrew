"""Worktree and permission isolation (milestone four)."""

from app.workspace.changes import (
    ChangedFile,
    ChangeKind,
    WorkspaceChangeCollector,
    WorkspaceChangeError,
    WorkspaceChangeSet,
)
from app.workspace.commands import (
    CommandExecutionError,
    CommandExecutor,
    CommandPolicy,
    CommandRequest,
    CommandResult,
    CommandRule,
    CommandStatus,
)
from app.workspace.permissions import (
    PathRole,
    PermissionGate,
    PermissionPolicy,
    PermissionReport,
    PermissionViolation,
    PermissionViolationKind,
    WorkspacePermissionError,
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
    "CommandExecutionError",
    "CommandExecutor",
    "CommandPolicy",
    "CommandRequest",
    "CommandResult",
    "CommandRule",
    "CommandStatus",
    "GitCommandError",
    "InvalidRepositoryError",
    "InvalidRevisionError",
    "PathRole",
    "PermissionGate",
    "PermissionPolicy",
    "PermissionReport",
    "PermissionViolation",
    "PermissionViolationKind",
    "WorkspaceChangeCollector",
    "WorkspaceChangeError",
    "WorkspaceChangeSet",
    "WorkspacePermissionError",
    "WorktreeConflictError",
    "WorktreeDirtyError",
    "WorktreeError",
    "WorktreeHandle",
    "WorktreeManager",
    "WorktreeNotFoundError",
]
