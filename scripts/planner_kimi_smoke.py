"""Bounded test harness for Planner -> clarification -> Plan v2 -> Implementer.

Uses the production chat runner, ArtifactStore and Verifier, but deliberately
does not run Reviewer, CompletionGuard or the full task event loop.
"""

import asyncio
import hashlib
import subprocess
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.agents import AgentAdapter, AgentEventType, AgentRegistry, AgentRole, PermissionMode
from app.orchestration.models import Task, TaskState
from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.team import (
    AgentTurnResult,
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
    default_team_personas,
)
from app.trace import TraceActorKind, TraceEvent, TraceEventType
from app.verification import (
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
    VerificationReport,
    VerificationStatus,
    Verifier,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    CommandRule,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeHandle,
    WorktreeManager,
)

BUGGY_SOURCE = "def total(items):\n    return sum(items[:-1])\n"
FIXED_SOURCE = "def total(items):\n    return sum(items)\n"
PUBLIC_TESTS = (
    "from src.pricing import total\n\n"
    "def test_total():\n    assert total([1, 2, 3]) == 6\n\n"
    "def test_empty():\n    assert total([]) == 0\n"
)
ISSUE = (
    "Fix src/pricing.py: total(items) must sum every item, including the last item. "
    "Empty input returns zero. Preserve the existing interface and modify only src/pricing.py. "
    "Do not edit tests or run shell commands as Implementer; CodeCrew runs tests independently. "
    "For this bounded integration exercise, Planner first publishes one Plan to Implementer. "
    "Implementer must read that Plan file and, before editing, ask Planner who runs the tests, "
    "including the received Plan artifact_id in the question's artifact_ids. "
    "Planner must answer with reply_to and publish Plan v2, explicitly stating that Verifier "
    "runs the tests. Implementer then reads Plan v2, implements it, and sends request_review "
    "to orchestrator. Each turn ends with finish_turn. No one claims task success."
)


@dataclass
class HandoffFixture:
    task: Task
    room: TeamRoom
    members: dict[MemberRole, RoomMember]
    store: ArtifactStore
    router: ConversationRouter
    runner: AgentTurnRunner
    manager: WorktreeManager
    handle: WorktreeHandle
    verification_plan: VerificationPlan
    verifier: Verifier
    agent_names: dict[MemberRole, str]
    turns: list[AgentTurnResult] = field(default_factory=list)

    async def turn(self, role: MemberRole) -> AgentTurnResult:
        member = self.members[role]
        attempt_id = uuid4()
        self.router.trace_store.append(
            TraceEvent(
                task_id=self.task.id,
                trace_id=self.task.trace_id,
                type=TraceEventType.AGENT_TURN_STARTED,
                actor_kind=TraceActorKind.AGENT,
                actor_id=str(member.member_id),
                payload={"role": role.value},
                idempotency_key=f"smoke-start:{attempt_id}",
            )
        )
        try:
            result = await self.runner.run(
                self.task,
                room_id=self.room.room_id,
                member_id=member.member_id,
                agent_name=self.agent_names[role],
                working_directory=(
                    self.handle.repository_root
                    if role is MemberRole.PLANNER
                    else self.handle.worktree_path
                ),
            )
        except Exception as exc:
            self.router.trace_store.append(
                TraceEvent(
                    task_id=self.task.id,
                    trace_id=self.task.trace_id,
                    type=TraceEventType.AGENT_TURN_FAILED,
                    actor_kind=TraceActorKind.AGENT,
                    actor_id=str(member.member_id),
                    payload={"role": role.value, "error_type": type(exc).__name__},
                    idempotency_key=f"smoke-failed:{attempt_id}",
                )
            )
            raise
        self.turns.append(result)
        evidence = self.store.put_json(
            result.model_dump(mode="json"),
            task_id=self.task.id,
            trace_id=self.task.trace_id,
            type=ArtifactType.GENERIC,
            created_by="handoff-smoke",
            filename=f"turn-{len(self.turns)}-{role.value}.json",
        )
        self.router.trace_store.append(
            TraceEvent(
                task_id=self.task.id,
                trace_id=self.task.trace_id,
                type=TraceEventType.AGENT_TURN_COMPLETED,
                actor_kind=TraceActorKind.AGENT,
                actor_id=str(member.member_id),
                payload={
                    "session_id": str(result.session.session_id),
                    "native_session_id": result.session.native_session_id,
                    "evidence_artifact_id": str(evidence.artifact_id),
                },
                idempotency_key=f"smoke-turn:{result.session.session_id}",
            )
        )
        return result


async def _git(repository: Path, *args: str) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        timeout=20,
    )


@asynccontextmanager
async def handoff_fixture(
    root: Path,
    planner: AgentAdapter,
    implementer_factory: Callable[[Path, Path, PermissionPolicy], AgentAdapter],
):
    repository = root / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src/__init__.py").write_text("", encoding="utf-8")
    (repository / "src/pricing.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (repository / "tests/test_pricing.py").write_text(PUBLIC_TESTS, encoding="utf-8")
    for args in (
        ("init", "-b", "main"),
        ("add", "."),
        (
            "-c",
            "user.name=CodeCrew Smoke",
            "-c",
            "user.email=smoke@codecrew.invalid",
            "commit",
            "-m",
            "buggy fixture",
        ),
    ):
        await _git(repository, *args)
    task = Task(issue=ISSUE, repository_path=str(repository))
    manager = WorktreeManager(root / "worktrees")
    handle = await manager.create(task_id=task.id, repository=repository)
    try:
        store = ArtifactStore(SQLiteDatabase(root / "trace.sqlite3"), root / "artifacts")
        store.initialize()
        rooms = TeamRoomStore(store.database)
        rooms.initialize()
        router = ConversationRouter(rooms, store)
        room_id = uuid4()
        personas = default_team_personas()
        members = {
            role: RoomMember(room_id=room_id, role=role, name=name, kind=kind)
            for role, name, kind in (
                (
                    MemberRole.PLANNER,
                    personas.for_role(MemberRole.PLANNER).display_name,
                    MemberKind.AGENT,
                ),
                (
                    MemberRole.IMPLEMENTER,
                    personas.for_role(MemberRole.IMPLEMENTER).display_name,
                    MemberKind.AGENT,
                ),
                (
                    MemberRole.REVIEWER,
                    personas.for_role(MemberRole.REVIEWER).display_name,
                    MemberKind.AGENT,
                ),
                (MemberRole.ORCHESTRATOR, "orchestrator", MemberKind.SYSTEM),
            )
        }
        room = TeamRoom(
            room_id=room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            name="Planner Kimi handoff smoke",
            members=tuple(members.values()),
        )
        rooms.create_room(room)
        policy = PermissionPolicy(allowed_paths=("src",))
        implementer = implementer_factory(manager.root, root / "kimi-runtime", policy)
        registry = AgentRegistry()
        registry.register(
            planner, roles={AgentRole.PLANNER}, permission_modes={PermissionMode.READ_ONLY}
        )
        registry.register(
            implementer,
            roles={AgentRole.IMPLEMENTER},
            permission_modes={PermissionMode.WORKSPACE_WRITE},
        )
        commands = (
            VerificationCommand(
                name="syntax",
                kind=VerificationCheckKind.STATIC_ANALYSIS,
                argv=(
                    sys.executable,
                    "-B",
                    "-c",
                    "import ast; from pathlib import Path; ast.parse(Path('src/pricing.py').read_text())",
                ),
            ),
            VerificationCommand(
                name="public",
                kind=VerificationCheckKind.PUBLIC_TESTS,
                argv=(
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_pricing.py",
                ),
            ),
            VerificationCommand(
                name="held_out",
                kind=VerificationCheckKind.HIDDEN_TESTS,
                argv=(
                    sys.executable,
                    "-B",
                    "-c",
                    "from src.pricing import total; assert total([-2, 3]) == 1; assert total([5]) == 5; assert total((2, 4)) == 6",
                ),
            ),
        )
        verifier = Verifier(
            store,
            WorkspaceChangeCollector(store),
            PermissionGate(store, policy),
            CommandExecutor(
                store,
                CommandPolicy(
                    rules=tuple(
                        CommandRule(
                            name=command.name, argv_prefix=command.argv, allow_extra_args=False
                        )
                        for command in commands
                    )
                ),
            ),
        )
        yield HandoffFixture(
            task,
            room,
            members,
            store,
            router,
            AgentTurnRunner(registry, router, timeout_seconds=180),
            manager,
            handle,
            VerificationPlan(commands=commands),
            verifier,
            {MemberRole.PLANNER: planner.name, MemberRole.IMPLEMENTER: implementer.name},
        )
    finally:
        await manager.remove(handle.task_id, force=True)


def _source_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("src", "tests")
        for path in (directory / folder).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _single_message(turn: AgentTurnResult, kind: MessageType):
    matches = [item for item in turn.routed_messages if item.message.type is kind]
    assert len(matches) == 1, f"expected exactly one {kind.value}"
    return matches[0].message


def _assert_plan_read(turn: AgentTurnResult, path: Path, working_directory: Path) -> None:
    def same_path(value):
        if not isinstance(value, str):
            return False
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = working_directory / candidate
        return candidate.resolve() == path.resolve()

    assert any(
        event.type is AgentEventType.TOOL_CALL
        and event.data.get("name") == "Read"
        and same_path(event.data.get("path"))
        for event in turn.events
    ), "Kimi did not provide a Read tool event for the authorized Plan path"


async def run_handoff(fixture: HandoffFixture) -> VerificationReport:
    """At most four Agent turns. Never marks the software task successful."""
    task, room = fixture.task, fixture.room
    original = _source_hashes(fixture.handle.repository_root)
    worktree_baseline = _source_hashes(fixture.handle.worktree_path)
    initial = await fixture.verifier.verify(
        fixture.handle, trace_id=task.trace_id, plan=fixture.verification_plan
    )
    assert not initial.passed, "buggy baseline unexpectedly passed"
    assert {VerificationCheckKind.PUBLIC_TESTS, VerificationCheckKind.HIDDEN_TESTS} <= {
        check.kind for check in initial.checks if check.status is VerificationStatus.FAILED
    }, "baseline must fail both public tests and held-out assertions"
    orchestrator = fixture.members[MemberRole.ORCHESTRATOR]
    fixture.router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=orchestrator.member_id,
            recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),),
            type=MessageType.ISSUE_POSTED,
            content=task.issue,
            idempotency_key="smoke-issue",
        ),
        authenticated_sender_id=orchestrator.member_id,
    )
    first = _single_message(await fixture.turn(MemberRole.PLANNER), MessageType.PLAN_SHARED)
    assert _source_hashes(fixture.handle.repository_root) == original
    assert _source_hashes(fixture.handle.worktree_path) == worktree_baseline
    plan1 = first.artifacts[0]
    question_turn = await fixture.turn(MemberRole.IMPLEMENTER)
    question = _single_message(question_turn, MessageType.QUESTION)
    assert plan1.artifact_id in {item.artifact_id for item in question.artifacts}
    _assert_plan_read(
        question_turn,
        fixture.store.blob_path_for(plan1.artifact_id).resolve(),
        fixture.handle.worktree_path,
    )
    assert _source_hashes(fixture.handle.worktree_path) == worktree_baseline
    clarified = await fixture.turn(MemberRole.PLANNER)
    answer = _single_message(clarified, MessageType.ANSWER)
    assert answer.reply_to == question.message_id
    assert answer.correlation_id == question.correlation_id
    revised = _single_message(clarified, MessageType.PLAN_SHARED)
    revisions = fixture.runner.rooms.list_plan_revisions(room.room_id)
    assert [item.version for item in revisions] == [1, 2]
    assert revisions[1].supersedes_artifact_id == plan1.artifact_id
    assert question.message_id in revisions[1].addresses_message_ids
    assert _source_hashes(fixture.handle.repository_root) == original
    assert _source_hashes(fixture.handle.worktree_path) == worktree_baseline
    edited = await fixture.turn(MemberRole.IMPLEMENTER)
    _single_message(edited, MessageType.IMPLEMENTATION_READY)
    _assert_plan_read(
        edited,
        fixture.store.blob_path_for(revised.artifacts[0].artifact_id).resolve(),
        fixture.handle.worktree_path,
    )
    final = await fixture.verifier.verify(
        fixture.handle, trace_id=task.trace_id, plan=fixture.verification_plan
    )
    assert final.passed, (
        f"deterministic checks failed: {[check.name for check in final.checks if check.status.value != 'passed']}"
    )
    assert [item.path for item in final.change_set.changed_files] == ["src/pricing.py"]
    assert _source_hashes(fixture.handle.repository_root) == original
    assert task.state is TaskState.CREATED  # Reviewer and CompletionGuard remain unrun.
    fixture.store.put_json(
        {
            "scope": "planner-implementer-handoff",
            "task_id": str(task.id),
            "trace_id": str(task.trace_id),
            "plan_ids": [str(item.artifact_id) for item in revisions],
            "session_ids": [str(turn.session.session_id) for turn in fixture.turns],
            "verification_artifact_id": str(final.artifact.artifact_id),
            "handoff_validation_passed": True,
            "task_success": False,
            "reviewer_run": False,
            "completion_guard_run": False,
        },
        task_id=task.id,
        trace_id=task.trace_id,
        type=ArtifactType.TASK_REPORT,
        created_by="handoff-smoke",
        filename="handoff-report.json",
    )
    return final
