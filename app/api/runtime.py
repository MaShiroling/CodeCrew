"""Explicit, policy-bearing composition root for the task API."""

from dataclasses import dataclass
from pathlib import Path

from app.agents import AgentCapability, AgentRegistry, AgentRole, PermissionMode
from app.api.persistent_service import PersistentTaskService
from app.config import Settings
from app.recovery import EvidenceRecoveryService, WorkflowRecoveryCoordinator
from app.storage import (
    ArtifactStore,
    RuntimeContextRepository,
    SQLiteDatabase,
    TaskRepository,
)
from app.team import (
    AgentTurnRunner,
    ConversationRouter,
    MemberRole,
    TeamRoomStore,
    WorkflowController,
    WorkflowDirectiveExecutor,
    WorkflowEventLoop,
)
from app.trace import TraceStore
from app.verification import (
    CompletionGuard,
    VerificationCheckKind,
    VerificationPlan,
    Verifier,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeManager,
)


@dataclass(frozen=True, slots=True)
class TaskRuntime:
    service: PersistentTaskService
    recovery: WorkflowRecoveryCoordinator


def build_task_runtime(
    *,
    settings: Settings,
    registry: AgentRegistry,
    agent_names: dict[MemberRole, str],
    verification_plan: VerificationPlan,
    permission_policy: PermissionPolicy,
    command_policy: CommandPolicy,
) -> TaskRuntime:
    """Build one runnable single-worker service from explicit Agent and safety policy."""
    if not settings.database_url.startswith("sqlite:///"):
        raise ValueError("only sqlite:/// database URLs are supported")
    database_path = settings.database_url.removeprefix("sqlite:///")
    if not database_path:
        raise ValueError("SQLite database path must not be empty")
    required = {
        MemberRole.PLANNER: (
            AgentRole.PLANNER, PermissionMode.READ_ONLY, AgentCapability.REPOSITORY_ANALYSIS
        ),
        MemberRole.IMPLEMENTER: (
            AgentRole.IMPLEMENTER, PermissionMode.WORKSPACE_WRITE, AgentCapability.CODE_EDIT
        ),
        MemberRole.REVIEWER: (
            AgentRole.REVIEWER, PermissionMode.READ_ONLY, AgentCapability.CODE_REVIEW
        ),
    }
    if set(agent_names) != set(required):
        raise ValueError("all three Agent role bindings are required")
    for role, (agent_role, permission, capability) in required.items():
        registry.resolve(
            agent_names[role],
            role=agent_role,
            permission_mode=permission,
            required_capabilities=(capability,),
        )
    if not verification_plan.commands:
        raise ValueError("a configured verification plan is required")
    categories = {command.kind for command in verification_plan.commands}
    required_categories = {
        VerificationCheckKind.PUBLIC_TESTS,
        VerificationCheckKind.HIDDEN_TESTS,
    }
    if not categories.intersection(
        {VerificationCheckKind.STATIC_ANALYSIS, VerificationCheckKind.BUILD}
    ) or not required_categories.issubset(categories):
        raise ValueError("verification plan requires static/build, public, and hidden checks")
    for command in verification_plan.commands:
        if not any(rule.matches(command.argv) for rule in command_policy.rules):
            raise ValueError(f"verification command {command.name!r} is not allowlisted")

    database = SQLiteDatabase(Path(database_path))
    tasks = TaskRepository(database)
    contexts = RuntimeContextRepository(database)
    artifacts = ArtifactStore(database, settings.artifact_root)
    rooms = TeamRoomStore(database)
    traces = TraceStore(database)
    router = ConversationRouter(rooms, artifacts, traces)
    turns = AgentTurnRunner(registry, router, timeout_seconds=settings.agent_timeout_seconds)
    verifier = Verifier(
        artifacts,
        WorkspaceChangeCollector(artifacts),
        PermissionGate(artifacts, permission_policy),
        CommandExecutor(artifacts, command_policy),
    )
    controller = WorkflowController(rooms, max_rework_rounds=settings.max_rework_rounds)
    executor = WorkflowDirectiveExecutor(
        turns=turns,
        router=router,
        verifier=verifier,
        completion_guard=CompletionGuard(artifacts),
        artifacts=artifacts,
    )
    event_loop = WorkflowEventLoop(controller, executor)
    worktrees = WorktreeManager(settings.worktree_root)
    service = PersistentTaskService(
        tasks=tasks,
        contexts=contexts,
        rooms=rooms,
        router=router,
        worktrees=worktrees,
        event_loop=event_loop,
        verification_plan=verification_plan,
        agent_names=agent_names,
    )
    recovery = WorkflowRecoveryCoordinator(
        tasks=tasks,
        contexts=contexts,
        rooms=rooms,
        traces=traces,
        worktrees=worktrees,
        evidence=EvidenceRecoveryService(artifacts, traces),
        event_loop=event_loop,
    )
    return TaskRuntime(service=service, recovery=recovery)
