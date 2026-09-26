"""Planner-only deadlines: simulated CLI, production guards, no real requests."""

import json

import pytest

from app.agents import CodexCliAdapter, FakeAgentScenario
from app.agents.process import ProcessResult
from app.team import AgentTurnError, AgentTurnRunner, MemberRole
from app.trace import TraceEventType
from scripts.planner_kimi_smoke import handoff_fixture
from scripts.smoke_evidence import archive_smoke_evidence
from scripts.three_agent_smoke import run_three_agent
from tests.test_agent_turn_runner import make_context
from tests.test_codex_adapter import StubProcess, StubRunner, json_chunk
from tests.test_planner_kimi_handoff import KimiProcessRunner, PlannerProcessRunner, factory
from tests.test_three_agent_success import ReviewerProcessRunner, reviewer_adapter


@pytest.mark.parametrize("value", [0, -1, 901, True, 1.5, "360", float("inf")])
def test_runner_rejects_invalid_planner_override(tmp_path, value):
    _, router, *_ = make_context(tmp_path, FakeAgentScenario())
    with pytest.raises(ValueError, match="planner_timeout_seconds"):
        AgentTurnRunner(None, router, planner_timeout_seconds=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [None, 360])
async def test_smoke_uses_role_deadlines_and_records_bounded_policy(tmp_path, deadline):
    planner, kimi, reviewer = PlannerProcessRunner(), KimiProcessRunner(), ReviewerProcessRunner()
    async with handoff_fixture(
        tmp_path, CodexCliAdapter(runner=planner), factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        result = await run_three_agent(fixture, planner_timeout_seconds=deadline)
        effective = deadline if deadline is not None else 180
        assert [options["timeout_seconds"] for _, options in planner.calls] == [effective] * 2
        assert [options["timeout_seconds"] for _, options in kimi.calls] == [180] * 2
        assert reviewer.calls[0][1]["timeout_seconds"] == 180
        assert result.runtime.latest_completion.passed
        report = fixture.store.read_json(result.report.artifact_id)
        policy = fixture.store.read_json(report["runtime_policy_artifact_id"])
        assert policy["planner_timeout_seconds"] == effective
        assert policy["max_agent_duration_ms"] == (effective * 2 + 180 * 3) * 1000
        assert policy["transport"] == "cli-default" and policy["automatic_workflow_retries"] == 0
        for role in (MemberRole.IMPLEMENTER, MemberRole.REVIEWER):
            assert fixture.runner.timeout_for_role(role) == 180


@pytest.mark.asyncio
async def test_timeout_keeps_policy_and_unacked_issue_in_failure_archive(tmp_path):
    process = StubProcess(
        [json_chunk({"type": "thread.started", "thread_id": "offline-timeout"})],
        ProcessResult(exit_code=-15, duration_ms=360016, timed_out=True),
    )
    planner = StubRunner(process)
    kimi, reviewer = KimiProcessRunner(), ReviewerProcessRunner()
    async with handoff_fixture(
        tmp_path, CodexCliAdapter(runner=planner), factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        with pytest.raises(AgentTurnError, match="timed_out"):
            await run_three_agent(fixture, planner_timeout_seconds=360)
        assert len(planner.calls) == 1 and planner.calls[0]["timeout_seconds"] == 360
        assert kimi.calls == reviewer.calls == []
        assert fixture.runner.rooms.pending_for(fixture.members[MemberRole.PLANNER].member_id)
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )
        archive = archive_smoke_evidence(fixture.store, fixture.task, root=tmp_path / "archives")
        manifest = json.loads((archive / "manifest.json").read_text())
        policies = [
            a for a in manifest["artifacts"]
            if a["metadata"].get("purpose") == "smoke-runtime-policy"
        ]
        assert len(policies) == 1
        a = policies[0]
        policy = json.loads(
            (archive / "artifacts/sha256" / a["sha256"][:2] / a["sha256"]).read_text()
        )
        assert policy["planner_timeout_seconds"] == 360


@pytest.mark.asyncio
async def test_smoke_invalid_deadline_stops_before_baseline_or_agent(tmp_path):
    planner, kimi, reviewer = PlannerProcessRunner(), KimiProcessRunner(), ReviewerProcessRunner()
    async with handoff_fixture(
        tmp_path, CodexCliAdapter(runner=planner), factory(kimi),
        reviewer=reviewer_adapter(reviewer),
    ) as fixture:
        with pytest.raises(ValueError, match="planner_timeout_seconds"):
            await run_three_agent(fixture, planner_timeout_seconds=901)
        assert planner.calls == kimi.calls == reviewer.calls == []
        with fixture.store.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
