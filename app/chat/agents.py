"""Private, repository-free execution context for one read-only Agent turn.

This module does not poll the mailbox or schedule turns. The 8.4d controller
will use its lifecycle methods after deciding whom to wake.
"""

import os
import shutil
import stat
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from app.agents import (
    AgentAdapter,
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    CodexCliAdapter,
    DeepSeekClaudeReviewerAdapter,
    KimiCodeAdapter,
    PermissionMode,
)
from app.config import Settings
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy

_ROLES = {
    MemberRole.PLANNER: AgentRole.PLANNER,
    MemberRole.IMPLEMENTER: AgentRole.IMPLEMENTER,
    MemberRole.REVIEWER: AgentRole.REVIEWER,
}


class StandaloneChatAgentError(RuntimeError):
    """The private, read-only chat execution boundary is unavailable."""


@dataclass(frozen=True, slots=True)
class ChatExecutionWorkspace:
    execution_id: UUID
    room_id: UUID
    path: Path
    runtime: Path


class StandaloneChatWorkspaceManager:
    """Creates exact, private directories outside any Git checkout."""

    def __init__(self, workspace_root: Path, runtime_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().absolute()
        self.runtime_root = runtime_root.expanduser().absolute()
        if (
            self.workspace_root == self.runtime_root
            or self.workspace_root.is_relative_to(self.runtime_root)
            or self.runtime_root.is_relative_to(self.workspace_root)
        ):
            raise ValueError("standalone chat workspace and runtime roots must be separate")
        self._ensure_root(self.workspace_root, reject_git=True)
        self._ensure_root(self.runtime_root, reject_git=True)

    @staticmethod
    def _ensure_root(path: Path, *, reject_git: bool) -> None:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise StandaloneChatAgentError("standalone chat root must not contain symlinks")
        if reject_git and any((part / ".git").exists() for part in (path, *path.parents)):
            raise StandaloneChatAgentError("standalone chat workspace must be outside Git repositories")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise StandaloneChatAgentError("standalone chat root must be private to its owner")

    def create(self, room_id: UUID) -> ChatExecutionWorkspace:
        self._ensure_root(self.workspace_root, reject_git=True)
        self._ensure_root(self.runtime_root, reject_git=True)
        execution_id = uuid4()
        workspace = self.workspace_root / str(execution_id)
        runtime = self.runtime_root / str(execution_id)
        workspace.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        (runtime / "home").mkdir(mode=0o700)
        (runtime / "tmp").mkdir(mode=0o700)
        return ChatExecutionWorkspace(execution_id, room_id, workspace, runtime)

    def cleanup(self, workspace: ChatExecutionWorkspace) -> None:
        for root, target in (
            (self.workspace_root, workspace.path), (self.runtime_root, workspace.runtime),
        ):
            self._ensure_root(root, reject_git=True)
            if target != root / str(workspace.execution_id) or target.is_symlink():
                raise StandaloneChatAgentError("refusing to clean an unmanaged chat directory")
            if target.exists():
                shutil.rmtree(target)


@dataclass(slots=True)
class _ActiveChatTurn:
    adapter: AgentAdapter
    workspace: ChatExecutionWorkspace


class StandaloneChatAgentRuntime:
    """Explicit start/stream/wait/cancel only; no automatic mailbox dispatch."""

    def __init__(
        self,
        workspaces: StandaloneChatWorkspaceManager,
        adapters: Mapping[MemberRole, AgentAdapter],
    ) -> None:
        if set(adapters) != set(_ROLES):
            raise ValueError("standalone chat requires planner, implementer and reviewer adapters")
        self.workspaces = workspaces
        self.adapters = dict(adapters)
        self._active: dict[UUID, _ActiveChatTurn] = {}
        self._completed: dict[UUID, AgentResult] = {}

    async def start(
        self, *, room_id: UUID, trace_id: UUID, role: MemberRole,
        prompt: str, timeout_seconds: int,
    ) -> AgentSession:
        if role not in _ROLES:
            raise StandaloneChatAgentError("only the three Agent roles can join a chat turn")
        if not prompt.strip() or timeout_seconds <= 0:
            raise ValueError("chat prompt and timeout must be valid before creating a workspace")
        workspace = self.workspaces.create(room_id)
        request = AgentRequest(
            task_id=workspace.execution_id,
            trace_id=trace_id,
            role=_ROLES[role],
            prompt=prompt,
            working_directory=workspace.path,
            runtime_directory=workspace.runtime,
            standalone_chat_room_id=room_id,
            permission_mode=PermissionMode.READ_ONLY,
            discussion_only=True,
            timeout_seconds=timeout_seconds,
        )
        adapter = self.adapters[role]
        # If start raises without returning a session, preserve the private
        # directory: the provider's process status may be unknown.
        session = await adapter.start(request)
        self._active[session.session_id] = _ActiveChatTurn(adapter, workspace)
        return session

    def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        return self._session(session_id).adapter.stream(session_id)

    async def wait(self, session_id: UUID) -> AgentResult:
        if result := self._completed.get(session_id):
            return result
        turn = self._session(session_id)
        result = await turn.adapter.wait(session_id)
        self.workspaces.cleanup(turn.workspace)
        self._active.pop(session_id, None)
        self._completed[session_id] = result
        return result

    async def cancel(self, session_id: UUID) -> AgentResult:
        if result := self._completed.get(session_id):
            return result
        await self._session(session_id).adapter.cancel(session_id)
        return await self.wait(session_id)

    def _session(self, session_id: UUID) -> _ActiveChatTurn:
        try:
            return self._active[session_id]
        except KeyError as exc:
            raise StandaloneChatAgentError("standalone chat session not found") from exc


def build_standalone_chat_agent_runtime(settings: Settings) -> StandaloneChatAgentRuntime:
    """Construct the three real read-only CLI bindings without starting a model."""
    workspaces = StandaloneChatWorkspaceManager(
        settings.standalone_chat_workspace_root, settings.standalone_chat_runtime_root,
    )
    return StandaloneChatAgentRuntime(workspaces, {
        MemberRole.PLANNER: CodexCliAdapter(executable=settings.codex_cli_path),
        MemberRole.IMPLEMENTER: KimiCodeAdapter(
            worktree_root=workspaces.workspace_root,
            runtime_root=workspaces.runtime_root,
            policy=PermissionPolicy(allowed_paths=(".",)),
            executable=settings.kimi_cli_path,
            allow_standalone_chat=True,
        ),
        MemberRole.REVIEWER: DeepSeekClaudeReviewerAdapter(executable=settings.claude_cli_path),
    })
