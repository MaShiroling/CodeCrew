"""Human-only contract and read-only preflight for a future chat-to-code handoff.

Preflight never creates a coding Task or grants execution authority. P3.2 must
recheck this contract, the Git baseline and the effective server-side policies
at the moment a Task is actually created.
"""

import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.chat.service import (
    ChatApiError,
    ChatConflict,
    ChatMessageNotFound,
    StandaloneChatService,
)
from app.chat.store import StandaloneChatMessageNotFoundError
from app.team.models import MemberRole, RoomStatus
from app.workspace.permissions import PermissionPolicy

_MAX_ALLOWED_PATHS = 16
_FORBIDDEN_SCOPE_PARTS = frozenset({".git", ".codecrew", ".env"})


class CodingPreflightInvalid(ChatApiError):
    code = "coding_preflight_invalid"
    status_code = 422


class CodingTaskDraft(BaseModel):
    """Human-selected source, repository, change goal and narrow write roots."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    source_message_id: UUID
    repository_path: str = Field(min_length=1, max_length=4_096)
    issue: str = Field(min_length=1, max_length=16_000)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=_MAX_ALLOWED_PATHS)

    @field_validator("repository_path")
    @classmethod
    def require_absolute_repository(cls, value: str) -> str:
        if not Path(value).is_absolute() or value == "/":
            raise ValueError("repository_path must be an absolute repository directory")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def validate_write_roots(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        normalized = PermissionPolicy(allowed_paths=paths).allowed_paths
        if len(normalized) != len(paths):
            raise ValueError("allowed_paths must be unique")
        for value in normalized:
            parts = PurePosixPath(value).parts
            if value == "." or _FORBIDDEN_SCOPE_PARTS.intersection(parts):
                raise ValueError("allowed_paths must name narrow, non-sensitive write roots")
        return normalized


class AuthorizeChatCodingTaskRequest(CodingTaskDraft):
    """Future P3.2 command; defining it does not expose an execution endpoint."""

    idempotency_key: UUID
    expected_base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    confirmation: Literal["authorize_one_coding_task"]


class CodingTaskPreflight(BaseModel):
    """Snapshot for Human review; it is not a grant or a completion result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_id: UUID
    trace_id: UUID
    source_message_id: UUID
    issue: str
    repository_path: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    allowed_paths: tuple[str, ...]
    execution_authorized: Literal[False] = False
    task_created: Literal[False] = False


class AuthorizedCodingTask(BaseModel):
    """Durable link from one Human authorization to one coding Task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_id: UUID
    source_message_id: UUID
    idempotency_key: UUID
    task_id: UUID
    task_trace_id: UUID
    repository_path: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    allowed_paths: tuple[str, ...]
    execution_authorized: Literal[True] = True
    task_created: Literal[True] = True


def preflight_coding_task(
    service: StandaloneChatService, room_id: UUID, draft: CodingTaskDraft,
) -> CodingTaskPreflight:
    """Validate a Human source and clean Git baseline without changing state."""
    room = service.get_room(room_id)
    if room.status is not RoomStatus.ACTIVE:
        raise ChatConflict("closed chat rooms cannot start coding tasks")
    try:
        source = service.store.get_message(draft.source_message_id).message
    except StandaloneChatMessageNotFoundError as exc:
        raise ChatMessageNotFound("coding source message is not in this room") from exc
    if source.room_id != room_id:
        raise ChatMessageNotFound("coding source message is not in this room")
    human = next(member for member in room.members if member.role is MemberRole.HUMAN)
    if source.sender_id != human.member_id:
        raise CodingPreflightInvalid("coding task source must be a Human message")

    try:
        candidate = Path(draft.repository_path).resolve()
    except (OSError, RuntimeError) as exc:
        raise CodingPreflightInvalid("repository path cannot be resolved") from exc
    if not candidate.is_dir():
        raise CodingPreflightInvalid("repository directory does not exist")
    inside = _git(candidate, "rev-parse", "--is-inside-work-tree")
    root = Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve()
    if inside != "true" or root != candidate:
        raise CodingPreflightInvalid("repository_path must name the Git worktree root")
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ChatConflict("repository has uncommitted or untracked changes")
    for allowed in draft.allowed_paths:
        try:
            resolved = (root / allowed).resolve()
        except (OSError, RuntimeError) as exc:
            raise CodingPreflightInvalid("allowed path cannot be resolved") from exc
        if not resolved.is_relative_to(root):
            raise CodingPreflightInvalid("allowed path escapes the repository")
    return CodingTaskPreflight(
        room_id=room_id, trace_id=room.trace_id,
        source_message_id=source.message_id, issue=draft.issue,
        repository_path=str(root), base_commit=commit,
        allowed_paths=draft.allowed_paths,
    )


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodingPreflightInvalid("unable to inspect Git repository") from exc
    if result.returncode != 0:
        raise CodingPreflightInvalid("repository is not a readable Git working tree")
    return result.stdout.strip()
