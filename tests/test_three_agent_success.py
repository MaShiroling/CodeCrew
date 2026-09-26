"""Production event loop and real CLI parsers, simulated model processes only."""

import json
from uuid import uuid4

import pytest

from app.agents import CodexCliAdapter, DeepSeekClaudeReviewerAdapter
from app.orchestration.models import TaskState
from app.storage import ArtifactReference, ArtifactType
from app.team import (
    AgentTurnError,
    ChatActionError,
    MemberRole,
    MessageType,
    WorkflowExecutionError,
)
from app.trace import TraceEventType
from app.verification import CompletionConditionKind
from scripts.planner_kimi_smoke import handoff_fixture
from scripts.three_agent_smoke import run_three_agent
from tests.test_planner_kimi_handoff import (
    KimiProcessRunner,
    PlannerProcessRunner,
    Process,
    factory,
    messages,
)


class ReviewerProcessRunner:
    def __init__(self, mode="approve"):
        self.mode = mode
        self.calls = []

    async def start(self, argv, **options):
        self.calls.append((argv, options))
        incoming = messages(argv[-1])
        evidence = next(item for item in incoming if item["type"] == "verification_ready")[
            "artifacts"
        ]
        from pathlib import Path

        assert {"plan", "verification_report", "changeset", "permission_report"} <= {
            ref["type"] for ref in evidence
        }
        # Read the actual blobs; never fabricate all input from chat summaries.
        for ref in evidence:
            Path(ref["path"]).read_bytes()
        events = [{"type": "system", "subtype": "init", "session_id": "independent-reviewer"}]
        read_evidence = evidence if self.mode != "missing_read" else evidence[:-1]
        events.extend(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": ref["path"]},
                        }
                    ]
                },
            }
            for ref in read_evidence
        )
        if self.mode == "write":
            (options["cwd"] / "src/pricing.py").write_text("def total(items):\n    return 0\n")
        elif self.mode == "tamper":
            Path(evidence[0]["path"]).write_text("tampered")
        elif self.mode == "forbidden_tool":
            events.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "true"},
                            }
                        ]
                    },
                }
            )
        issues = []
        if self.mode in {"reject", "high_issue"}:
            issues = [
                {
                    "issue_id": str(uuid4()),
                    "priority": "high",
                    "summary": "Evidence needs work",
                    "resolved": False,
                }
            ]
        action = {
            "action": "request_rework" if self.mode == "reject" else "approve_review",
            "recipient": {"kind": "role", "role": "orchestrator"},
            "content": "Independent review based on supplied artifacts",
            "artifact_content": {"issues": issues},
        }
        answer = json.dumps(
            {"actions": [action, {"action": "finish_turn", "content": "Review ended"}]}
        )
        if self.mode == "prose":
            answer = f"Reviewed and approved.\n```json\n{answer}\n```"
        elif self.mode == "high_issue":
            answer = f"Approved despite the issues.\n```json\n{answer}\n```"
        elif self.mode == "ambiguous":
            answer = f"```json\n{answer}\n```\n```json\n{answer}\n```"
        events.append({"type": "result", "session_id": "independent-reviewer", "result": answer})
        return Process(events)


def reviewer_adapter(runner):
    return DeepSeekClaudeReviewerAdapter(
        runner=runner,
        env_source={"DEEPSEEK_API_KEY": "offline-placeholder"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_production_event_loop_completes_only_after_review_and_guard(tmp_path, wrapped):
    planner = PlannerProcessRunner()
    kimi = KimiProcessRunner(response_style="prose_prefix" if wrapped else "raw")
    reviewer = ReviewerProcessRunner("prose" if wrapped else "approve")
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=planner),
        factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        result = await run_three_agent(fixture)
        assert fixture.task.state is TaskState.COMPLETED
        assert result.runtime.latest_completion.passed
        assert len(planner.calls) == len(kimi.calls) == 2
        assert len(reviewer.calls) == 1
        argv, options = reviewer.calls[0]
        assert "--resume" not in argv and "--tools=Read,Glob,Grep" in argv
        assert options["env"]["ANTHROPIC_AUTH_TOKEN"] == "offline-placeholder"
        assert "KIMI_MODEL_API_KEY" not in options["env"]
        review_turn = result.workflow.agent_turns[-1]
        assert review_turn.session.native_session_id == "independent-reviewer"
        assert all(
            turn.session.session_id != review_turn.session.session_id
            for turn in result.workflow.agent_turns[:-1]
        )
        evidence_message = next(
            item.message
            for item in fixture.runner.rooms.list_messages(fixture.room.room_id)
            if item.message.type is MessageType.VERIFICATION_READY
        )
        assert len({ref.artifact_id for ref in evidence_message.artifacts}) == len(
            evidence_message.artifacts
        )
        latest_plan = fixture.runner.rooms.latest_plan_revision(fixture.room.room_id)
        assert latest_plan.artifact_id in {ref.artifact_id for ref in evidence_message.artifacts}
        assert len(result.runtime.latest_completion.conditions) == 10
        for role in (
            MemberRole.PLANNER,
            MemberRole.IMPLEMENTER,
            MemberRole.REVIEWER,
            MemberRole.ORCHESTRATOR,
        ):
            assert fixture.runner.rooms.pending_for(fixture.members[role].member_id) == ()
        trace = fixture.router.trace_store.list(trace_id=fixture.task.trace_id, limit=1000)
        types = [item.event.type for item in trace]
        assert TraceEventType.VERIFICATION_COMPLETED in types
        assert TraceEventType.COMPLETION_DECIDED in types
        assert types.index(TraceEventType.VERIFICATION_COMPLETED) < types.index(
            TraceEventType.COMPLETION_DECIDED
        )
        report = fixture.store.read_json(result.report.artifact_id)
        assert report["task_success"] is True
        assert report["patch_artifact_id"] and report["review_artifact_ids"]
        assert report["sessions"][1]["token_usage"] is None
        assert report["reviewer_os_sandbox"] is report["hidden_test_secrecy"] is False


@pytest.mark.asyncio
async def test_reviewer_approval_cannot_replace_valid_diff_or_tests(tmp_path):
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(KimiProcessRunner(edit=False)),
        reviewer=reviewer_adapter(ReviewerProcessRunner()),
    ) as fixture:
        result = await run_three_agent(fixture)
        assert fixture.task.state is TaskState.NEEDS_HUMAN
        assert result.workflow.paused
        decision = result.runtime.latest_completion
        assert not decision.passed
        assert {
            CompletionConditionKind.VALID_DIFF,
            CompletionConditionKind.PUBLIC_TESTS,
            CompletionConditionKind.HIDDEN_TESTS,
        } <= set(decision.failed_conditions)
        assert fixture.store.read_json(result.report.artifact_id)["task_success"] is False


@pytest.mark.asyncio
async def test_reviewer_rejection_pauses_without_automatic_rework(tmp_path):
    kimi, reviewer = KimiProcessRunner(), ReviewerProcessRunner("reject")
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        result = await run_three_agent(fixture)
        assert fixture.task.state is TaskState.NEEDS_HUMAN
        assert result.runtime.latest_completion is None
        assert result.workflow.paused and len(kimi.calls) == 2 and len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_oversized_review_evidence_fails_before_launch(tmp_path, monkeypatch):
    reviewer = ReviewerProcessRunner()
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(KimiProcessRunner()),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        verify = fixture.verifier.verify
        calls = 0

        async def oversized(*args, **kwargs):
            nonlocal calls
            report = await verify(*args, **kwargs)
            calls += 1
            if calls == 1:  # Preserve the real failing baseline.
                return report
            extra = tuple(
                ArtifactReference.from_metadata(
                    fixture.store.put_text(
                        f"evidence {index}",
                        task_id=fixture.task.id,
                        trace_id=fixture.task.trace_id,
                        type=ArtifactType.TEST_LOG,
                        created_by="offline-test",
                    ),
                    summary="extra test log",
                )
                for index in range(51)
            )
            check = report.checks[0].model_copy(update={"evidence": extra})
            return report.model_copy(update={"checks": (check, *report.checks[1:])})

        monkeypatch.setattr(fixture.verifier, "verify", oversized)
        with pytest.raises(WorkflowExecutionError, match="reference limit"):
            await run_three_agent(fixture)
        assert reviewer.calls == []
        assert fixture.task.state is TaskState.VERIFYING
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error", "message"),
    [
        ("missing_read", WorkflowExecutionError, "visibly read"),
        ("forbidden_tool", WorkflowExecutionError, "unapproved tool"),
        ("write", WorkflowExecutionError, "changed the workspace"),
        ("tamper", AgentTurnError, "Artifact changed"),
        ("high_issue", AgentTurnError, "high-priority"),
        ("ambiguous", ChatActionError, "not valid JSON"),
    ],
)
async def test_review_integrity_failures_cannot_complete(tmp_path, mode, error, message):
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(KimiProcessRunner()),
        reviewer=reviewer_adapter(ReviewerProcessRunner(mode)),
    ) as fixture:
        with pytest.raises(error, match=message):
            await run_three_agent(fixture)
        assert fixture.task.state is TaskState.REVIEWING
        assert not fixture.task.is_terminal
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )
        assert fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.AGENT_TURN_FAILED
        )
