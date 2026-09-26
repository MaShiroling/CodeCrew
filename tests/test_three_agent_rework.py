"""Fault-injection experiments with real verification, simulated model processes."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import CodexCliAdapter
from app.orchestration.models import TaskState
from app.recovery import EvidenceRecoveryService
from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.team import AgentTurnError, MemberRole, MessageType, WorkflowExecutionError
from app.trace import TraceEventType, TraceStore
from scripts.planner_kimi_smoke import FIXED_SOURCE, handoff_fixture
from scripts.smoke_evidence import archive_smoke_evidence
from scripts.three_agent_smoke import run_three_agent
from tests.test_planner_kimi_handoff import (
    KimiProcessRunner,
    PlannerProcessRunner,
    Process,
    factory,
    messages,
)
from tests.test_three_agent_success import ReviewerProcessRunner, reviewer_adapter


class ReworkKimiRunner(KimiProcessRunner):
    async def start(self, argv, **options):
        if len(self.calls) < 2:
            return await super().start(argv, **options)
        self.calls.append((argv, options))
        incoming = messages(argv[argv.index("--prompt") + 1])
        evidence = [ref for item in incoming for ref in item["artifacts"]]
        assert {"review_report", "verification_report", "plan"} <= {ref["type"] for ref in evidence}
        for ref in evidence:
            Path(ref["path"]).read_bytes()
        (options["cwd"] / "src/pricing.py").write_text(FIXED_SOURCE)
        action = {
            "action": "request_review",
            "recipient": {"kind": "role", "role": "orchestrator"},
            "content": "Fixed review findings; Verifier runs tests independently",
        }
        return Process(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "Read", "arguments": {"path": ref["path"]}}}
                        for ref in evidence
                    ],
                },
                {
                    "role": "assistant",
                    "content": json.dumps({"actions": [action, {"action": "finish_turn"}]}),
                },
            ]
        )


class ReworkReviewerRunner(ReviewerProcessRunner):
    def __init__(self, *, drop_prior=False, false_approve=False):
        super().__init__()
        self.issue_id = str(uuid4())
        self.drop_prior = drop_prior
        self.false_approve = false_approve

    async def start(self, argv, **options):
        evidence = messages(argv[-1])[-1]["artifacts"]
        report = json.loads(
            Path(
                next(ref["path"] for ref in evidence if ref["type"] == "verification_report")
            ).read_text()
        )
        self.mode = "approve" if report["passed"] or self.false_approve else "reject"
        process = await super().start(argv, **options)
        native_id = f"independent-reviewer-{len(self.calls)}"
        process.events[0]["session_id"] = native_id
        result = process.events[-1]
        result["session_id"] = native_id
        turn = json.loads(result["result"])
        turn["actions"][0]["artifact_content"]["issues"] = (
            []
            if (self.false_approve or (self.drop_prior and report["passed"]))
            else [
                {
                    "issue_id": self.issue_id,
                    "priority": "high",
                    "summary": "total adds an extra one; public and held-out tests fail",
                    "resolved": report["passed"],
                }
            ]
        )
        result["result"] = json.dumps(turn)
        return process


@pytest.mark.asyncio
@pytest.mark.parametrize("response_style", ["raw", "bare_tail"])
@pytest.mark.parametrize(
    "scenario,turns,rounds,reviews",
    [
        ("rework_success", 7, 1, 2),
        ("rework_exhaustion", 9, 2, 3),
    ],
)
async def test_rework_and_two_round_escalation_use_production_controller(
    tmp_path, scenario, turns, rounds, reviews, response_style
):
    planner = PlannerProcessRunner()
    kimi = ReworkKimiRunner(response_style=response_style)
    reviewer = ReworkReviewerRunner()
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=planner),
        factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        result = await run_three_agent(fixture, scenario=scenario)
        assert fixture.task.rework_rounds == rounds
        assert len(result.workflow.agent_turns) == turns
        assert len(planner.calls) == 2 and len(kimi.calls) == 2 + rounds
        assert len(reviewer.calls) == reviews
        assert all("--resume" not in argv for argv, _ in reviewer.calls)
        assert (
            len(
                {
                    turn.session.native_session_id
                    for turn in result.workflow.agent_turns
                    if turn.session.role.value == "reviewer"
                }
            )
            == reviews
        )
        report = fixture.store.read_json(result.report.artifact_id)
        assert report["fault_injection_experiment"] and report["rework_budget"] == 2
        assert report["rework_rounds"] == rounds
        stored = fixture.runner.rooms.list_messages(fixture.room.room_id)
        rework_handoffs = [
            item.message for item in stored if item.message.type is MessageType.SYSTEM_EVENT
        ]
        assert len(rework_handoffs) == rounds
        assert all(
            {ArtifactType.REVIEW_REPORT, ArtifactType.PLAN, ArtifactType.VERIFICATION_REPORT}
            <= {ref.type for ref in m.artifacts}
            for m in rework_handoffs
        )
        for handoff in rework_handoffs:
            parent = fixture.runner.rooms.get_message(handoff.causation_id).message
            assert parent.type is MessageType.REWORK_REQUEST
            assert handoff.correlation_id == parent.correlation_id
        trace = fixture.router.trace_store.list(trace_id=fixture.task.trace_id, limit=1000)
        assert sum(e.event.type is TraceEventType.VERIFICATION_COMPLETED for e in trace) == reviews
        assert sum(e.event.type is TraceEventType.TEST_FAULT_INJECTED for e in trace) == (
            1 if rounds == 1 else 3
        )
        archive = archive_smoke_evidence(fixture.store, fixture.task, root=tmp_path / "archives")
        database = SQLiteDatabase(archive / "trace.sqlite3")
        recovered = EvidenceRecoveryService(
            ArtifactStore(database, archive / "artifacts"), TraceStore(database)
        ).recover(task_id=fixture.task.id, trace_id=fixture.task.trace_id)
        if scenario == "rework_success":
            assert fixture.task.state is TaskState.COMPLETED and report["task_success"]
            assert recovered.completion and recovered.completion.passed
            assert recovered.review.issues[0].issue_id.hex == reviewer.issue_id.replace("-", "")
            assert recovered.review.issues[0].resolved
            assert (
                len(fixture.runner.rooms.pending_for(fixture.members[MemberRole.HUMAN].member_id))
                == 1
            )
        else:
            assert fixture.task.state is TaskState.NEEDS_HUMAN and not report["task_success"]
            assert result.workflow.pause_reason == "rework budget exhausted"
            assert recovered.completion is None and not recovered.review.issues[0].resolved
            human = fixture.runner.rooms.pending_for(fixture.members[MemberRole.HUMAN].member_id)
            assert len(human) == 1 and human[0].message.type is MessageType.HUMAN_INPUT_REQUEST
            assert any(ref.type is ArtifactType.REVIEW_REPORT for ref in human[0].message.artifacts)
            assert not any(e.event.type is TraceEventType.COMPLETION_DECIDED for e in trace)


@pytest.mark.asyncio
async def test_false_approval_of_injected_fault_stops_before_another_paid_turn(tmp_path):
    kimi, reviewer = ReworkKimiRunner(), ReworkReviewerRunner(false_approve=True)
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        with pytest.raises(WorkflowExecutionError, match="did not reject"):
            await run_three_agent(fixture, scenario="rework_success")
        assert fixture.task.state is TaskState.REVIEWING
        assert len(kimi.calls) == 2 and len(reviewer.calls) == 1
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )


@pytest.mark.asyncio
async def test_rework_approval_cannot_drop_unresolved_issue_ids(tmp_path):
    reviewer = ReworkReviewerRunner(drop_prior=True)
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(ReworkKimiRunner()),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        with pytest.raises(AgentTurnError, match="carry forward"):
            await run_three_agent(fixture, scenario="rework_success")
        assert fixture.task.state is TaskState.REVIEWING and fixture.task.rework_rounds == 1
        assert fixture.runner.rooms.pending_for(fixture.members[MemberRole.REVIEWER].member_id)
