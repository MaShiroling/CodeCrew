import subprocess
import sys
from pathlib import Path

import pytest

from app.agents import (
    AgentCapability,
    AgentRegistry,
    AgentRole,
    FakeAgentAdapter,
    FakeAgentScenario,
    PermissionMode,
)
from app.orchestration.models import Task, TaskState
from app.storage import ArtifactStore, SQLiteDatabase
from app.team import (
    AgentTurnRunner,
    ChatMessage,
    ConversationRouter,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    TeamRoom,
    TeamRoomStore,
    WorkflowController,
    WorkflowDirectiveExecutor,
    WorkflowEventLoop,
    WorkflowRuntime,
)
from app.verification import (
    CompletionGuard,
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
    Verifier,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    CommandRule,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeManager,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "src").mkdir()
    (repository / "src/app.py").write_text("value = 1\n")
    (repository / "verify.py").write_text("print('ok')\n")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "initial",
    )
    return repository


def verification_plan() -> VerificationPlan:
    return VerificationPlan(
        commands=tuple(
            VerificationCommand(
                name=name,
                kind=kind,
                argv=(sys.executable, "verify.py"),
            )
            for name, kind in (
                ("static", VerificationCheckKind.STATIC_ANALYSIS),
                ("public", VerificationCheckKind.PUBLIC_TESTS),
                ("hidden", VerificationCheckKind.HIDDEN_TESTS),
            )
        )
    )


@pytest.mark.asyncio
async def test_event_loop_runs_agents_verifier_and_completion_guard(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    task = Task(issue="Set value to two", repository_path=str(repository))
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    rooms = TeamRoomStore(database)
    artifacts.initialize()
    rooms.initialize()
    worktree = await WorktreeManager(tmp_path / "worktrees").create(
        task_id=task.id, repository=repository
    )
    (worktree.worktree_path / "src/app.py").write_text("value = 2\n")

    room_id = task.id

    def member(name: str, role: MemberRole, kind: MemberKind):
        return RoomMember(room_id=room_id, name=name, role=role, kind=kind)

    members = {
        MemberRole.PLANNER: member("planner", MemberRole.PLANNER, MemberKind.AGENT),
        MemberRole.IMPLEMENTER: member(
            "implementer", MemberRole.IMPLEMENTER, MemberKind.AGENT
        ),
        MemberRole.REVIEWER: member("reviewer", MemberRole.REVIEWER, MemberKind.AGENT),
        MemberRole.VERIFIER: member("verifier", MemberRole.VERIFIER, MemberKind.SYSTEM),
        MemberRole.ORCHESTRATOR: member(
            "orchestrator", MemberRole.ORCHESTRATOR, MemberKind.SYSTEM
        ),
        MemberRole.HUMAN: member("human", MemberRole.HUMAN, MemberKind.HUMAN),
    }
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="End-to-end room",
        members=tuple(members.values()),
    )
    rooms.create_room(room)
    planner = FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "actions": [
                    {
                        "action": "share_plan",
                        "recipient": {"kind": "role", "role": "implementer"},
                        "content": "Implementation plan",
                        "artifact_content": {
                            "steps": ["edit src/app.py", "run tests"]
                        },
                    },
                    {"action": "finish_turn", "content": "Plan ready"},
                ]
            }
        ),
        name="fake-planner",
        capabilities=frozenset({AgentCapability.REPOSITORY_ANALYSIS}),
    )
    implementer = FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "actions": [
                    {
                        "action": "request_review",
                        "recipient": {"kind": "role", "role": "orchestrator"},
                        "content": "Implementation ready for deterministic verification",
                    },
                    {"action": "finish_turn", "content": "Implementation ready"},
                ]
            }
        ),
        name="fake-implementer",
        capabilities=frozenset({AgentCapability.CODE_EDIT}),
    )
    reviewer = FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "actions": [
                    {
                        "action": "approve_review",
                        "recipient": {"kind": "role", "role": "orchestrator"},
                        "content": "Implementation satisfies the issue",
                        "artifact_content": {"issues": []},
                    },
                    {"action": "finish_turn", "content": "Approved"},
                ]
            }
        ),
        name="fake-reviewer",
        capabilities=frozenset({AgentCapability.CODE_REVIEW}),
    )
    registry = AgentRegistry()
    registry.register(
        planner,
        roles={AgentRole.PLANNER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    registry.register(
        implementer,
        roles={AgentRole.IMPLEMENTER},
        permission_modes={PermissionMode.WORKSPACE_WRITE},
    )
    registry.register(
        reviewer,
        roles={AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    router = ConversationRouter(rooms, artifacts)
    turns = AgentTurnRunner(registry, router)
    verifier = Verifier(
        artifacts,
        WorkspaceChangeCollector(artifacts),
        PermissionGate(artifacts, PermissionPolicy(allowed_paths=("src",))),
        CommandExecutor(
            artifacts,
            CommandPolicy(
                rules=(
                    CommandRule(
                        name="verification-script",
                        argv_prefix=(sys.executable, "verify.py"),
                    ),
                )
            ),
        ),
    )
    controller = WorkflowController(rooms)
    controller.initialize()
    executor = WorkflowDirectiveExecutor(
        turns=turns,
        router=router,
        verifier=verifier,
        completion_guard=CompletionGuard(artifacts),
        artifacts=artifacts,
    )
    loop = WorkflowEventLoop(controller, executor)
    issue = router.route(
        ChatMessage(
            room_id=room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=members[MemberRole.HUMAN].member_id,
            recipients=(
                MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),
            ),
            type=MessageType.ISSUE_POSTED,
            content=task.issue,
            idempotency_key="issue-posted",
        ),
        authenticated_sender_id=members[MemberRole.HUMAN].member_id,
    )
    runtime = WorkflowRuntime(
        task=task,
        room_id=room_id,
        worktree=worktree,
        verification_plan=verification_plan(),
        agent_names={
            MemberRole.PLANNER: planner.name,
            MemberRole.IMPLEMENTER: implementer.name,
            MemberRole.REVIEWER: reviewer.name,
        },
    )

    result = await loop.run(runtime, (issue,))

    assert result.task.state is TaskState.COMPLETED
    assert not result.paused
    assert result.processed_events == 6
    assert len(result.agent_turns) == 3
    assert runtime.latest_verification is not None
    assert runtime.latest_verification.passed
    assert runtime.latest_completion is not None
    assert runtime.latest_completion.passed
    types = [
        item.message.type for item in rooms.list_messages(room_id)
    ]
    assert types == [
        MessageType.ISSUE_POSTED,
        MessageType.PLAN_SHARED,
        MessageType.IMPLEMENTATION_READY,
        MessageType.VERIFICATION_READY,
        MessageType.REVIEW_APPROVED,
        MessageType.COMPLETION_PASSED,
    ]
    assert rooms.pending_for(members[MemberRole.ORCHESTRATOR].member_id) == ()
    assert len(rooms.pending_for(members[MemberRole.HUMAN].member_id)) == 1
