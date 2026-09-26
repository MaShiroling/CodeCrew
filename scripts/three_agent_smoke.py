"""Bounded disposable integration exercises using the production event loop.

No UI/server lifecycle, natural error-rate experiment, OS-level Reviewer sandbox, or secret
hidden-test isolation is established by this harness.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.agents import AgentEventType
from app.agents.timeouts import validate_planner_timeout
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
    WorkflowDirectiveExecutor,
    WorkflowEventLoop,
    WorkflowExecutionError,
    WorkflowRunResult,
    WorkflowRuntime,
)
from app.trace import TraceActorKind, TraceEvent, TraceEventType
from app.verification import CompletionGuard, VerificationCheckKind, VerificationStatus
from scripts.planner_kimi_smoke import HandoffFixture, _assert_plan_read, _source_hashes


def _snapshot(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            result[str(path.relative_to(root))] = f"symlink:{path.readlink()}"
        elif path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[str(path.relative_to(root))] = "directory"
    return result


class _EvidenceTurnRunner(AgentTurnRunner):
    """Check fixed-fixture evidence before the executor can invoke the guard."""

    def __init__(
        self, fixture: HandoffFixture, *, rework_pairs=0, inject_count=0,
        planner_timeout_seconds: int | None = None,
    ) -> None:
        super().__init__(
            fixture.runner.registry, fixture.router, timeout_seconds=180,
            planner_timeout_seconds=planner_timeout_seconds,
        )
        self.fixture = fixture
        self.original = _source_hashes(fixture.handle.repository_root)
        self.attempts = 0
        self.rework_pairs = rework_pairs
        self.inject_count = inject_count
        self.implementations = 0
        self.reviews = 0
        self.fault_artifacts = []
        self.reviewer_sessions = set()

    async def run(self, task, **kwargs):
        member = self.rooms.get_member(kwargs["member_id"])
        expected = (
            MemberRole.PLANNER,
            MemberRole.IMPLEMENTER,
            MemberRole.PLANNER,
            MemberRole.IMPLEMENTER,
            MemberRole.REVIEWER,
        ) + (MemberRole.IMPLEMENTER, MemberRole.REVIEWER) * self.rework_pairs
        if self.attempts >= len(expected) or member.role is not expected[self.attempts]:
            raise WorkflowExecutionError("bounded smoke budget or role order exceeded")
        self.attempts += 1
        readonly = member.role in {MemberRole.PLANNER, MemberRole.REVIEWER} or self.attempts == 2
        worktree_before = _snapshot(self.fixture.handle.worktree_path)
        evidence_before = (
            _snapshot(self.fixture.store.root) if member.role is MemberRole.REVIEWER else {}
        )
        pending = self.rooms.pending_for(member.member_id)
        # Match the already-validated handoff: each bounded planning turn gets a
        # fresh session and structured context, rather than adding a resume variant.
        if member.role is MemberRole.PLANNER:
            kwargs["resume_native_session_id"] = None
        if member.role is MemberRole.REVIEWER and kwargs.get("resume_native_session_id"):
            if self.rework_pairs:
                kwargs["resume_native_session_id"] = None
            else:
                raise WorkflowExecutionError("Reviewer must use an independent fresh session")
        turn = await super().run(task, **kwargs)
        if _source_hashes(self.fixture.handle.repository_root) != self.original:
            raise WorkflowExecutionError("original repository changed during an Agent turn")
        if readonly and _snapshot(self.fixture.handle.worktree_path) != worktree_before:
            raise WorkflowExecutionError("read-only or clarification turn changed the workspace")
        if member.role is MemberRole.IMPLEMENTER:
            plan = next(
                ref
                for message in pending
                for ref in message.message.artifacts
                if ref.type is ArtifactType.PLAN
            )
            _assert_plan_read(
                turn,
                self.fixture.store.blob_path_for(plan.artifact_id),
                self.fixture.handle.worktree_path,
            )
        if member.role is MemberRole.REVIEWER:
            self.reviews += 1
            after = _snapshot(self.fixture.store.root)
            if any(after.get(path) != digest for path, digest in evidence_before.items()):
                raise WorkflowExecutionError("Reviewer changed existing evidence")
            if not turn.session.native_session_id:
                raise WorkflowExecutionError("Reviewer returned no native session identifier")
            if turn.session.native_session_id in self.reviewer_sessions:
                raise WorkflowExecutionError("Reviewer reused a prior native session")
            self.reviewer_sessions.add(turn.session.native_session_id)
            calls = [event.data for event in turn.events if event.type is AgentEventType.TOOL_CALL]
            if any(call.get("name") not in {"Read", "Glob", "Grep"} for call in calls):
                raise WorkflowExecutionError("Reviewer attempted an unapproved tool")
            paths = set()
            for call in calls:
                args = call.get("input")
                path = args.get("file_path", args.get("path")) if isinstance(args, dict) else None
                if call.get("name") == "Read" and isinstance(path, str):
                    candidate = Path(path)
                    if not candidate.is_absolute():
                        candidate = self.fixture.handle.worktree_path / candidate
                    paths.add(candidate.resolve())
            required = {
                self.fixture.store.blob_path_for(ref.artifact_id).resolve()
                for message in pending
                for ref in message.message.artifacts
            }
            if not required or not required <= paths:
                raise WorkflowExecutionError(
                    "Reviewer did not visibly read every supplied evidence Artifact"
                )
            if self.reviews <= self.inject_count and not any(
                item.message.type is MessageType.REWORK_REQUEST for item in turn.routed_messages
            ):
                raise WorkflowExecutionError("Reviewer did not reject the injected defect")
        self.fixture.store.put_json(
            turn.model_dump(mode="json"),
            task_id=task.id,
            trace_id=task.trace_id,
            type=ArtifactType.GENERIC,
            created_by="three-agent-smoke",
            filename=f"turn-{self.attempts}-{member.role.value}.json",
        )
        if member.role is MemberRole.IMPLEMENTER and any(
            item.message.type is MessageType.IMPLEMENTATION_READY for item in turn.routed_messages
        ):
            self.implementations += 1
            if self.implementations <= self.inject_count:
                self._inject_fault(task)
        return turn

    def _inject_fault(self, task):
        # Test-only mutation of the disposable worktree, before deterministic verification.
        path = self.fixture.handle.worktree_path / "src/pricing.py"
        if not path.is_file() or path.resolve() != (
            self.fixture.handle.worktree_path.resolve() / "src/pricing.py"
        ) or path.is_symlink():
            raise WorkflowExecutionError("fault injection requires the regular fixture source file")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        content = b"def total(items):\n    return sum(items) + 1\n"
        path.write_bytes(content)
        metadata = self.fixture.store.put_json(
            {
                "path": "src/pricing.py", "implementation": self.implementations,
                "before_sha256": before, "injected_sha256": hashlib.sha256(content).hexdigest(),
                "fault": "test-only extra +1 violates totals and empty-input requirements",
            },
            task_id=task.id, trace_id=task.trace_id, type=ArtifactType.GENERIC,
            created_by="smoke-fault-injector", filename=f"fault-{self.implementations}.json",
            metadata={"purpose": "test-fault-injection"},
        )
        self.fault_artifacts.append(str(metadata.artifact_id))
        self.fixture.router.trace_store.append(TraceEvent(
            task_id=task.id, trace_id=task.trace_id, type=TraceEventType.TEST_FAULT_INJECTED,
            actor_kind=TraceActorKind.DETERMINISTIC, actor_id="smoke-fault-injector",
            idempotency_key=f"test-fault:{metadata.artifact_id}",
            payload={"artifact_id": str(metadata.artifact_id), "implementation": self.implementations},
        ))


@dataclass(frozen=True)
class ThreeAgentResult:
    workflow: WorkflowRunResult
    runtime: WorkflowRuntime
    report: ArtifactReference


async def run_three_agent(
    fixture: HandoffFixture, *, scenario="success", planner_timeout_seconds: int | None = None,
) -> ThreeAgentResult:
    """Run all roles via production directives; never set task success manually."""
    if MemberRole.REVIEWER not in fixture.agent_names:
        raise WorkflowExecutionError("Reviewer adapter is required")
    scenarios = {"success": (0, 0), "rework_success": (1, 1), "rework_exhaustion": (2, 3)}
    if scenario not in scenarios:
        raise ValueError("unknown three-agent smoke scenario")
    rework_pairs, inject_count = scenarios[scenario]
    max_turns = 5 + 2 * rework_pairs
    validate_planner_timeout(planner_timeout_seconds)
    planner_timeout = planner_timeout_seconds if planner_timeout_seconds is not None else 180
    duration_budget_ms = (2 * planner_timeout + (max_turns - 2) * 180) * 1000
    policy = fixture.store.put_json(
        {
            "schema_version": 1, "trace_id": str(fixture.task.trace_id), "scenario": scenario,
            "planner_timeout_seconds": planner_timeout,
            "implementer_timeout_seconds": 180, "reviewer_timeout_seconds": 180,
            "max_agent_turns": max_turns, "max_agent_duration_ms": duration_budget_ms,
            "transport": "cli-default", "automatic_workflow_retries": 0,
        },
        task_id=fixture.task.id, trace_id=fixture.task.trace_id, type=ArtifactType.GENERIC,
        created_by="three-agent-smoke", filename="smoke-runtime-policy.json",
        metadata={"purpose": "smoke-runtime-policy"},
    )
    baseline = await fixture.verifier.verify(
        fixture.handle,
        trace_id=fixture.task.trace_id,
        plan=fixture.verification_plan,
    )
    assert not baseline.passed
    assert {VerificationCheckKind.PUBLIC_TESTS, VerificationCheckKind.HIDDEN_TESTS} <= {
        check.kind for check in baseline.checks if check.status is VerificationStatus.FAILED
    }, "buggy baseline must fail public tests and held-out assertions"
    fixture.runner = _EvidenceTurnRunner(
        fixture, rework_pairs=rework_pairs, inject_count=inject_count,
        planner_timeout_seconds=planner_timeout_seconds,
    )
    rework_budget = 2 if rework_pairs else 0
    controller = WorkflowController(fixture.runner.rooms, max_rework_rounds=rework_budget)
    controller.initialize()
    executor = WorkflowDirectiveExecutor(
        turns=fixture.runner,
        router=fixture.router,
        verifier=fixture.verifier,
        completion_guard=CompletionGuard(fixture.store),
        artifacts=fixture.store,
        budget_guard=ConversationBudgetGuard(
            fixture.runner.rooms,
            ConversationBudgetPolicy(
                max_agent_turns=max_turns,
                max_reported_tokens=400_000 if rework_pairs else 200_000,
                max_agent_duration_ms=duration_budget_ms,
            ),
        ),
    )
    runtime = WorkflowRuntime(
        task=fixture.task,
        room_id=fixture.room.room_id,
        worktree=fixture.handle,
        verification_plan=fixture.verification_plan,
        agent_names=fixture.agent_names,
    )
    sender = fixture.members[MemberRole.ORCHESTRATOR]
    issue = fixture.router.route(
        ChatMessage(
            room_id=fixture.room.room_id,
            task_id=fixture.task.id,
            trace_id=fixture.task.trace_id,
            sender_id=sender.member_id,
            recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),),
            type=MessageType.ISSUE_POSTED,
            content=fixture.task.issue,
            idempotency_key="three-agent-issue",
        ),
        authenticated_sender_id=sender.member_id,
    )
    workflow = await WorkflowEventLoop(
        controller, executor, max_events=40 if rework_pairs else 20
    ).run(runtime, (issue,))
    revisions = fixture.runner.rooms.list_plan_revisions(fixture.room.room_id)
    assert [item.version for item in revisions] == [1, 2]
    assert revisions[1].supersedes_artifact_id == revisions[0].artifact_id
    messages = fixture.runner.rooms.list_messages(fixture.room.room_id)
    question = next(item.message for item in messages if item.message.type is MessageType.QUESTION)
    answer = next(item.message for item in messages if item.message.type is MessageType.ANSWER)
    assert (
        answer.reply_to == question.message_id and answer.correlation_id == question.correlation_id
    )
    assert question.message_id in revisions[1].addresses_message_ids
    assert runtime.latest_verification is not None
    guard = runtime.latest_completion
    success = fixture.task.state is TaskState.COMPLETED
    if success:
        assert [item.path for item in runtime.latest_verification.change_set.changed_files] == [
            "src/pricing.py"
        ]
        assert guard is not None and guard.passed
        assert not workflow.paused and len(workflow.agent_turns) == max_turns
        assert any(item.message.type is MessageType.REVIEW_APPROVED for item in messages)
        assert any(item.message.type is MessageType.COMPLETION_PASSED for item in messages)
    if rework_pairs:
        rejections = [item for item in messages if item.message.type is MessageType.REWORK_REQUEST]
        assert len(rejections) == inject_count
        assert len(workflow.agent_turns) == max_turns
        assert fixture.task.rework_rounds == rework_pairs
        assert fixture.runner.attempts == max_turns
        assert len(fixture.runner.fault_artifacts) == inject_count
        if scenario == "rework_success":
            assert success and runtime.latest_verification.passed
        else:
            assert fixture.task.state is TaskState.NEEDS_HUMAN and workflow.paused
            assert workflow.pause_reason == "rework budget exhausted"
            assert not runtime.latest_verification.passed and guard is None
            assert fixture.runner.rooms.pending_for(fixture.members[MemberRole.HUMAN].member_id)
    metadata = fixture.store.put_json(
        {
            "scope": f"three-agent-{scenario.replace('_', '-')}-path",
            "runtime_policy_artifact_id": str(policy.artifact_id),
            "task_id": str(fixture.task.id),
            "trace_id": str(fixture.task.trace_id),
            "state": fixture.task.state.value,
            "task_success": success,
            "plan_ids": [str(item.artifact_id) for item in revisions],
            "verification_artifact_id": str(runtime.latest_verification.artifact.artifact_id),
            "patch_artifact_id": str(
                runtime.latest_verification.change_set.diff_artifact.artifact_id
            )
            if runtime.latest_verification.change_set.diff_artifact
            else None,
            "completion_artifact_id": str(guard.artifact.artifact_id) if guard else None,
            "review_artifact_ids": [
                str(ref.artifact_id)
                for item in messages
                if item.message.type in {MessageType.REVIEW_APPROVED, MessageType.REWORK_REQUEST}
                for ref in item.message.artifacts
            ],
            "sessions": [
                {
                    "role": turn.session.role.value,
                    "session_id": str(turn.session.session_id),
                    "native_session_id": turn.session.native_session_id,
                    "duration_ms": turn.agent_result.duration_ms,
                    "token_usage": turn.agent_result.token_usage.model_dump(mode="json")
                    if turn.agent_result.token_usage
                    else None,
                }
                for turn in workflow.agent_turns
            ],
            "paused": workflow.paused,
            "pause_reason": workflow.pause_reason,
            "failed_conditions": [kind.value for kind in guard.failed_conditions]
            if guard
            else None,
            "reviewer_os_sandbox": False,
            "hidden_test_secrecy": False,
            "rework_budget": rework_budget,
            "rework_rounds": fixture.task.rework_rounds,
            "fault_injection_artifact_ids": fixture.runner.fault_artifacts,
            "fault_injection_experiment": bool(inject_count),
        },
        task_id=fixture.task.id,
        trace_id=fixture.task.trace_id,
        type=ArtifactType.TASK_REPORT,
        created_by="three-agent-smoke",
        filename="three-agent-report.json",
    )
    return ThreeAgentResult(
        workflow,
        runtime,
        ArtifactReference.from_metadata(metadata, summary="Three Agent integration report"),
    )
