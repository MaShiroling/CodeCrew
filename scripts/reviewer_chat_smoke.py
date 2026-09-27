"""Reviewer-only native chat contract acceptance against real disposable evidence.

The fixture writes both code variants itself. No Planner/Implementer model turn,
full event loop, CompletionGuard decision or security isolation claim is made.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from app.agents import AgentAdapter, AgentCapability
from app.agents.fake import FakeAgentAdapter
from app.orchestration.models import TaskState
from app.storage import ArtifactReference, ArtifactType
from app.team import (
    AgentTurnRunner,
    ChatMessage,
    ConversationBudgetGuard,
    ConversationBudgetPolicy,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    WorkflowController,
    WorkflowDirective,
    WorkflowDirectiveExecutor,
    WorkflowDirectiveKind,
    WorkflowExecutionError,
    WorkflowRuntime,
)
from app.team.actions import ChatActionType
from app.verification import CompletionGuard, VerificationCheckKind, VerificationStatus
from scripts.planner_kimi_smoke import FIXED_SOURCE, HandoffFixture, handoff_fixture
from scripts.reviewer_evidence import check_reviewer_evidence
from scripts.three_agent_smoke import _snapshot


class _UnusedAgent(FakeAgentAdapter):
    async def start(self, request):
        raise WorkflowExecutionError("Reviewer-only smoke cannot start Planner or Implementer")


@asynccontextmanager
async def reviewer_chat_fixture(root: Path, reviewer: AgentAdapter):
    planner = _UnusedAgent(
        name="fixed-plan-fixture", capabilities=frozenset({AgentCapability.REPOSITORY_ANALYSIS})
    )

    def implementer(*args):
        return _UnusedAgent(
            name="fixed-code-fixture", capabilities=frozenset({AgentCapability.CODE_EDIT})
        )

    async with handoff_fixture(root, planner, implementer, reviewer=reviewer) as fixture:
        fixture.task.issue = (
            "Fix src/pricing.py total(items) to sum every input item, including the last. "
            "Empty input returns zero. Preserve the interface and change only src/pricing.py; "
            "do not edit tests. This is a Reviewer-only evidence exercise: code and Plan are "
            "controlled fixtures, not outputs of a Planner/Implementer model. "
            "Review only the actual supplied evidence; never claim task completion."
        )
        fixture.runner = AgentTurnRunner(
            fixture.runner.registry,
            fixture.router,
            timeout_seconds=180,
            reviewer_structured_output=True,
        )
        yield fixture


def _message(fixture, sender_role, recipient_role, kind, content, *, artifacts=(), source=None):
    sender = fixture.members[sender_role]
    message = ChatMessage(
        room_id=fixture.room.room_id,
        task_id=fixture.task.id,
        trace_id=fixture.task.trace_id,
        sender_id=sender.member_id,
        recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=recipient_role),),
        type=kind,
        content=content,
        artifacts=artifacts,
        correlation_id=source.message.correlation_id if source else uuid4(),
        causation_id=source.message.message_id if source else None,
        idempotency_key=f"reviewer-smoke:{uuid4()}",
    )
    return fixture.router.route(message, authenticated_sender_id=sender.member_id)


async def run_reviewer_chat(fixture: HandoffFixture, *, scenario: str):
    if scenario not in {"approval", "rework"} or fixture.turns:
        raise ValueError("fresh Reviewer fixture and supported scenario required")
    plan = fixture.store.put_json(
        {
            "steps": ["Sum every item without an offset; empty input returns zero"],
            "allowed_paths": ["src"],
            "verification_owner": "Verifier",
            "origin": "controlled Reviewer-only fixture; not a Planner model response",
        },
        task_id=fixture.task.id,
        trace_id=fixture.task.trace_id,
        type=ArtifactType.PLAN,
        created_by="reviewer-chat-fixture",
        filename="fixed-plan.json",
    )
    _message(
        fixture,
        MemberRole.PLANNER,
        MemberRole.IMPLEMENTER,
        MessageType.PLAN_SHARED,
        "Controlled fixture plan",
        artifacts=(ArtifactReference.from_metadata(plan, summary="Fixed fixture Plan"),),
    )
    controller = WorkflowController(fixture.runner.rooms)
    controller.initialize()
    for state in (TaskState.PLANNING, TaskState.IMPLEMENTING, TaskState.VERIFYING):
        fixture.task.transition_to(state)
    runtime = WorkflowRuntime(
        fixture.task,
        fixture.room.room_id,
        fixture.handle,
        fixture.verification_plan,
        fixture.agent_names,
    )
    executor = WorkflowDirectiveExecutor(
        turns=fixture.runner,
        router=fixture.router,
        verifier=fixture.verifier,
        completion_guard=CompletionGuard(fixture.store),
        artifacts=fixture.store,
        budget_guard=ConversationBudgetGuard(
            fixture.runner.rooms, ConversationBudgetPolicy(max_agent_turns=2)
        ),
    )
    source = _message(
        fixture,
        MemberRole.IMPLEMENTER,
        MemberRole.ORCHESTRATOR,
        MessageType.IMPLEMENTATION_READY,
        "Controlled fixture edit; verify independently",
    )
    sessions, reviews, prior_ids = set(), [], set()
    for index in range(1 if scenario == "approval" else 2):
        fixed = scenario == "approval" or index == 1
        # Test-only code changes, never a model edit or a rewritten review result.
        (fixture.handle.worktree_path / "src/pricing.py").write_text(
            FIXED_SOURCE if fixed else "def total(items):\n    return sum(items) + 1\n",
            encoding="utf-8",
        )
        await executor.execute(
            WorkflowDirective(
                kind=WorkflowDirectiveKind.RUN_VERIFIER, reason="Reviewer fixture evidence"
            ),
            source=source,
            runtime=runtime,
        )
        verification = runtime.latest_verification
        if verification is None or verification.passed is not fixed:
            raise WorkflowExecutionError(
                "controlled code variant has unexpected verification result"
            )
        for kind in (VerificationCheckKind.PUBLIC_TESTS, VerificationCheckKind.HIDDEN_TESTS):
            expected = VerificationStatus.PASSED if fixed else VerificationStatus.FAILED
            if not any(
                check.kind is kind and check.status is expected for check in verification.checks
            ):
                raise WorkflowExecutionError(
                    "fixture public/held-out tests have unexpected results"
                )
        if verification.change_set.diff_artifact is None:
            raise WorkflowExecutionError("Reviewer fixture requires a real Diff")
        fixture.task.transition_to(TaskState.REVIEWING)
        if index:
            _message(
                fixture,
                MemberRole.ORCHESTRATOR,
                MemberRole.REVIEWER,
                MessageType.SYSTEM_EVENT,
                "Inspect the prior review, carry every issue ID, and determine whether each is resolved",
                artifacts=source.message.artifacts,
                source=source,
            )
        member = fixture.members[MemberRole.REVIEWER]
        pending = fixture.runner.rooms.pending_for(member.member_id)
        required = {
            fixture.store.blob_path_for(ref.artifact_id)
            for item in pending
            for ref in item.message.artifacts
        }
        worktree_before = _snapshot(fixture.handle.worktree_path)
        repository_before = _snapshot(fixture.handle.repository_root)
        evidence_before = _snapshot(fixture.store.root)
        def validate(
            candidate, parsed, *, worktree_before=worktree_before,
            repository_before=repository_before, evidence_before=evidence_before,
            required=required, fixed=fixed,
        ):
            if (
                _snapshot(fixture.handle.worktree_path) != worktree_before
                or _snapshot(fixture.handle.repository_root) != repository_before
            ):
                raise WorkflowExecutionError("Reviewer changed the workspace or original repository")
            after = _snapshot(fixture.store.root)
            if any(after.get(path) != digest for path, digest in evidence_before.items()):
                raise WorkflowExecutionError("Reviewer changed existing evidence")
            check_reviewer_evidence(
                candidate, working_directory=fixture.handle.worktree_path,
                required_paths=required, native_sessions=sessions, native_output=True,
            )
            expected = ChatActionType.APPROVE_REVIEW if fixed else ChatActionType.REQUEST_REWORK
            actions = [action for action in parsed.actions if action.action in {
                ChatActionType.APPROVE_REVIEW, ChatActionType.REQUEST_REWORK,
            }]
            if len(actions) != 1 or actions[0].action is not expected:
                raise WorkflowExecutionError("Reviewer decision contradicts the controlled evidence")

        turn = await fixture.turn(MemberRole.REVIEWER, validate_before_routing=validate)
        expected_kind = MessageType.REVIEW_APPROVED if fixed else MessageType.REWORK_REQUEST
        decisions = [
            item
            for item in turn.routed_messages
            if item.message.type
            in {
                MessageType.REVIEW_APPROVED,
                MessageType.REWORK_REQUEST,
            }
        ]
        if len(decisions) != 1 or decisions[0].message.type is not expected_kind:
            raise WorkflowExecutionError("Reviewer decision contradicts the controlled evidence")
        source = decisions[0]
        reference = next(
            ref for ref in source.message.artifacts if ref.type is ArtifactType.REVIEW_REPORT
        )
        report = fixture.store.read_json(reference.artifact_id)
        issues = {item["issue_id"]: item for item in report["issues"]}
        if not fixed:
            prior_ids = {issue_id for issue_id, issue in issues.items() if not issue["resolved"]}
            if not prior_ids:
                raise WorkflowExecutionError("rejection must identify unresolved issues")
            controller.handle(fixture.task, source)  # Route state only; never wake an Implementer.
            fixture.task.transition_to(TaskState.VERIFYING)
        elif prior_ids and (
            not prior_ids <= issues.keys() or any(not issues[key]["resolved"] for key in prior_ids)
        ):
            raise WorkflowExecutionError("approval must explicitly resolve every prior issue ID")
        reviews.append(
            {
                "review_artifact_id": str(reference.artifact_id),
                "verdict": report["verdict"],
                "issue_ids": list(issues),
                "native_session_id": turn.session.native_session_id,
                "verification_artifact_id": str(verification.artifact.artifact_id),
            }
        )
    if runtime.latest_completion is not None or fixture.task.state is not TaskState.REVIEWING:
        raise WorkflowExecutionError("Reviewer-only smoke cannot determine task completion")
    metadata = fixture.store.put_json(
        {
            "scope": "reviewer-chat-native-acceptance",
            "scenario": scenario,
            "trace_id": str(fixture.task.trace_id),
            "task_state": fixture.task.state.value,
            "reviewer_acceptance_passed": True,
            "reviewer_turns": len(reviews),
            "planner_implementer_turns": 0,
            "task_completion_evaluated": False,
            "fixture_edits_only": True,
            "reviews": reviews,
            "reviewer_os_readonly_isolation": False,
            "hidden_test_secrecy": False,
        },
        task_id=fixture.task.id,
        trace_id=fixture.task.trace_id,
        type=ArtifactType.GENERIC,
        created_by="reviewer-chat-smoke",
        filename="reviewer-chat-acceptance.json",
    )
    return metadata
