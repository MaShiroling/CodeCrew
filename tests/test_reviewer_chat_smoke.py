"""Real Verifier and native CLI parsing, simulated Reviewer process only."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.agents import DeepSeekClaudeReviewerAdapter
from app.agents.process import ProcessResult
from app.orchestration.models import TaskState
from app.team import AgentTurnError, ChatActionError, MemberRole, WorkflowExecutionError
from app.trace import TraceEventType
from scripts.replay_reviewer import replay_archive
from scripts.reviewer_chat_smoke import reviewer_chat_fixture, run_reviewer_chat
from scripts.smoke_evidence import archive_smoke_evidence
from tests.test_planner_kimi_handoff import Process, messages


class _Process(Process):
    def __init__(self, events, exit_code=0):
        super().__init__(events)
        self.exit_code = exit_code

    async def wait(self):
        return ProcessResult(exit_code=self.exit_code, duration_ms=1)


class NativeReviewerProcess:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.calls = []
        self.outputs = []
        self.issue_id = str(uuid4())

    async def start(self, argv, **options):
        self.calls.append((argv, options))
        assert "--resume" not in argv and "--json-schema" in argv
        assert options["env"]["MAX_STRUCTURED_OUTPUT_RETRIES"] == "1"
        assert options["timeout_seconds"] == 180
        assert "KIMI_MODEL_API_KEY" not in options["env"]
        assert "OPENAI_API_KEY" not in options["env"]
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        assert schema == json.loads(argv[-1].split("Action schema:\n")[1].split("\n\n")[0])
        incoming = messages(argv[-1])
        refs = {ref["artifact_id"]: ref for item in incoming for ref in item["artifacts"]}
        assert {"plan", "diff", "verification_report", "test_log", "command_audit"} <= {
            ref["type"] for ref in refs.values()
        }
        checklist = json.loads(argv[-1].split("Required Read checklist", 1)[1].split(
            ":\n", 1
        )[1].split("\n\n", 1)[0])
        assert {entry["path"] for entry in checklist} == {ref["path"] for ref in refs.values()}
        assert len(checklist) == len({ref["path"] for ref in refs.values()})
        assert "Reading stdout/stderr does NOT replace" in argv[-1]
        for ref in refs.values():
            Path(ref["path"]).read_bytes()
        verification = json.loads(
            Path(
                next(ref["path"] for ref in refs.values() if ref["type"] == "verification_report")
            ).read_text()
        )
        passed = verification["passed"]
        prior = [ref for ref in refs.values() if ref["type"] == "review_report"]
        issues = (
            []
            if passed and not prior
            else [
                {
                    "issue_id": self.issue_id,
                    "priority": "high",
                    "summary": "Offset fixed" if passed else "Adds an unwanted offset",
                    "resolved": passed,
                }
            ]
        )
        if self.mode == "drop_prior" and passed:
            issues = []
        if self.mode == "wrong_verdict":
            passed = not passed
            issues = (
                []
                if passed
                else [
                    {
                        "issue_id": self.issue_id,
                        "priority": "high",
                        "summary": "Synthetic rejection",
                        "resolved": False,
                    }
                ]
            )
        native = {
            "actions": [
                {
                    "action": "approve_review" if passed else "request_rework",
                    "recipient": {"kind": "role", "role": "orchestrator"},
                    "content": "Independent evidence inspected",
                    "artifact_content": {"issues": issues},
                },
                {"action": "finish_turn"},
            ]
        }
        valid_text = json.dumps(native)
        if self.mode == "missing_report":
            native["actions"][0].pop("artifact_content")
        elif self.mode == "invalid_native":
            native = None
        elif self.mode == "write":
            (options["cwd"] / "src/pricing.py").write_text("def total(items):\n    return 0\n")
        elif self.mode == "tamper":
            Path(next(iter(refs.values()))["path"]).write_text("tampered evidence")
        native_id = (
            "reused-reviewer"
            if self.mode == "reused_session"
            else f"independent-reviewer-{len(self.calls)}"
        )
        reads = list(refs.values())
        if self.mode == "missing_reads":
            reads = reads[:-1]
        elif self.mode == "missing_public_audit":
            # Reconstruct observed 4d6a635f failure, not a byte-exact archived response.
            omitted = next(ref["path"] for ref in refs.values() if ref["type"] == "command_audit"
                           and "tests/test_pricing.py" in json.loads(Path(ref["path"]).read_text())["argv"])
            reads = [ref for ref in reads if ref["path"] != omitted]
            assert any(ref["type"] == "test_log" for ref in reads)
        events = [{"type": "system", "subtype": "init", "session_id": native_id}]
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
            for ref in reads
        )
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash" if self.mode == "extra_tool" else "StructuredOutput",
                            "input": {} if self.mode == "wrong_formatter" else native,
                        }
                    ]
                },
            }
        )
        payload = {
            "type": "result",
            "subtype": "success",
            "session_id": native_id,
            "structured_output": native,
            "result": valid_text
            if self.mode in {"invalid_native", "missing_report"}
            else "Malformed text is not the source {",
        }
        events.append(payload)
        self.outputs.append(payload)
        return _Process(events, exit_code=1 if self.mode == "nonzero" else 0)


def reviewer_adapter(process):
    return DeepSeekClaudeReviewerAdapter(
        runner=process,
        env_source={
            "DEEPSEEK_API_KEY": "offline-reviewer-placeholder",
            "PATH": "/usr/bin",
            "KIMI_MODEL_API_KEY": "not-forwarded",
            "OPENAI_API_KEY": "not-forwarded",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,count", [("approval", 1), ("rework", 2)])
async def test_reviewer_only_native_cases_use_real_evidence_and_fresh_sessions(
    tmp_path, scenario, count
):
    process = NativeReviewerProcess()
    async with reviewer_chat_fixture(tmp_path, reviewer_adapter(process)) as fixture:
        metadata = await run_reviewer_chat(fixture, scenario=scenario)
        report = fixture.store.read_json(metadata.artifact_id)
        assert report["reviewer_acceptance_passed"] and report["reviewer_turns"] == count
        assert report["planner_implementer_turns"] == 0 and not report["task_completion_evaluated"]
        assert report["fixture_edits_only"] and not report["reviewer_os_readonly_isolation"]
        assert not report["hidden_test_secrecy"]
        assert len(process.calls) == count and len(fixture.turns) == count
        assert len({turn.session.native_session_id for turn in fixture.turns}) == count
        assert fixture.task.state is TaskState.REVIEWING
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )
        if scenario == "rework":
            assert fixture.task.rework_rounds == 1
            assert (
                report["reviews"][0]["issue_ids"]
                == report["reviews"][1]["issue_ids"]
                == [process.issue_id]
            )
            first = fixture.store.read_json(UUID(report["reviews"][0]["review_artifact_id"]))
            second = fixture.store.read_json(UUID(report["reviews"][1]["review_artifact_id"]))
            assert first["issues"][0]["resolved"] is False
            assert second["issues"][0]["resolved"] is True
        archive = archive_smoke_evidence(fixture.store, fixture.task, root=tmp_path / "archives")
        replay = replay_archive(archive, source="native")
        assert len(replay["cases"]) == count
        assert all(case["decision"] == "accepted" for case in replay["cases"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,error,message",
    [
        ("missing_report", ChatActionError, "contract"),
        ("invalid_native", ChatActionError, "structured_output"),
        ("nonzero", AgentTurnError, "failed"),
        ("missing_reads", WorkflowExecutionError, "visibly read"),
        ("missing_public_audit", WorkflowExecutionError, "visibly read"),
        ("extra_tool", WorkflowExecutionError, "unapproved tool"),
        ("wrong_formatter", WorkflowExecutionError, "unapproved tool"),
        ("write", WorkflowExecutionError, "changed the workspace"),
        ("tamper", AgentTurnError, "Artifact changed"),
        ("wrong_verdict", WorkflowExecutionError, "contradicts"),
    ],
)
async def test_reviewer_smoke_failures_do_not_retry_or_declare_success(
    tmp_path, mode, error, message
):
    process = NativeReviewerProcess(mode)
    async with reviewer_chat_fixture(tmp_path, reviewer_adapter(process)) as fixture:
        with pytest.raises(error, match=message):
            await run_reviewer_chat(fixture, scenario="approval")
        assert len(process.calls) == 1 and not fixture.task.is_terminal
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )
        assert fixture.runner.rooms.pending_for(fixture.members[MemberRole.REVIEWER].member_id)
        assert not any(item.message.type.value in {"review_approved", "rework_request"}
                       for item in fixture.runner.rooms.list_messages(fixture.room.room_id))
        with fixture.store.database.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE artifact_type='review_report'"
            ).fetchone()[0] == 0
        recorded = fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.AGENT_OUTPUT_RECORDED
        )
        saved = fixture.store.read_json(UUID(recorded[-1].event.payload["artifact_id"]))
        assert saved["output"]["structured_output"] == process.outputs[-1]["structured_output"]
        with fixture.store.database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE filename='reviewer-chat-acceptance.json'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,error,message",
    [
        ("drop_prior", AgentTurnError, "carry forward"),
        ("reused_session", WorkflowExecutionError, "reused"),
    ],
)
async def test_second_review_must_carry_ids_and_use_a_new_session(tmp_path, mode, error, message):
    process = NativeReviewerProcess(mode)
    async with reviewer_chat_fixture(tmp_path, reviewer_adapter(process)) as fixture:
        with pytest.raises(error, match=message):
            await run_reviewer_chat(fixture, scenario="rework")
        assert len(process.calls) == 2 and fixture.task.state is TaskState.REVIEWING
        assert not fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.COMPLETION_DECIDED
        )


@pytest.mark.asyncio
async def test_unsupported_scenario_starts_no_agent(tmp_path):
    process = NativeReviewerProcess()
    async with reviewer_chat_fixture(tmp_path, reviewer_adapter(process)) as fixture:
        with pytest.raises(ValueError, match="supported scenario"):
            await run_reviewer_chat(fixture, scenario="unknown")
        assert not process.calls
