"""Offline handoff using production adapters with simulated CLI processes."""

import json
from typing import ClassVar

import pytest

from app.agents import CodexCliAdapter, KimiCodeAdapter
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.team import (
    AgentTurnError,
    ChatActionError,
    ChatMessage,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
)
from app.trace import TraceEventType
from scripts.planner_kimi_smoke import FIXED_SOURCE, handoff_fixture, run_handoff


class Process:
    def __init__(self, events):
        self.events = events

    async def stream(self):
        for event in self.events:
            yield ProcessChunk(ProcessStream.STDOUT, json.dumps(event) + "\n")

    async def wait(self):
        return ProcessResult(exit_code=0, duration_ms=1)

    async def cancel(self):
        return None


def messages(prompt):
    return json.loads(prompt.split("New messages:\n", 1)[1].split("\n\nPlan history:", 1)[0])


class PlannerProcessRunner:
    def __init__(self):
        self.calls = []

    async def start(self, argv, **options):
        self.calls.append((argv, options))
        actions = []
        incoming = messages(argv[-1])
        question = next((item for item in incoming if item["type"] == "question"), None)
        if question:
            actions.append(
                {
                    "action": "answer_question",
                    "recipient": {"kind": "role", "role": "implementer"},
                    "reply_to": question["message_id"],
                    "content": "Verifier runs tests.",
                }
            )
        plan = {"steps": ["Read src/pricing.py", "sum all items without slicing"]}
        if question:
            plan["verification_owner"] = "Verifier"
        actions.extend(
            [
                {
                    "action": "share_plan",
                    "recipient": {"kind": "role", "role": "implementer"},
                    "content": "Implement this plan",
                    "artifact_content": plan,
                },
                {"action": "finish_turn", "content": "Plan sent"},
            ]
        )
        return Process(
            [
                {"type": "thread.started", "thread_id": f"offline-planner-{len(self.calls)}"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps({"actions": actions})},
                },
                {"type": "turn.completed"},
            ]
        )


class KimiProcessRunner:
    def __init__(self, *, edit=True, response_style="raw"):
        self.calls = []
        self.edit = edit
        self.response_style = response_style
        self.outputs = []

    async def start(self, argv, **options):
        self.calls.append((argv, options))
        incoming = messages(argv[argv.index("--prompt") + 1])
        plan = next(item for item in incoming if item["type"] == "plan_shared")["artifacts"][0]
        from pathlib import Path

        # Simulate reading the exact file named in the real prompt, not inline plan text.
        assert json.loads(Path(plan["path"]).read_text())["steps"]
        if len(self.calls) == 1:
            action = {
                "action": "ask_question",
                "recipient": {"kind": "role", "role": "planner"},
                "content": "Who runs the tests?",
                "artifact_ids": [plan["artifact_id"]],
            }
        else:
            if self.edit:
                (options["cwd"] / "src/pricing.py").write_text(FIXED_SOURCE, encoding="utf-8")
            action = {
                "action": "request_review",
                "recipient": {"kind": "role", "role": "orchestrator"},
                "content": "Implementation ready; Verifier must run tests",
            }
        raw = json.dumps(
            {"actions": [action, {"action": "finish_turn", "content": "Turn ended"}]},
            ensure_ascii=False,
        )
        fenced = f"```json\n{raw}\n```"
        responses = {
            "raw": raw,
            "fenced": fenced,
            "prose_prefix": f"我已阅读 Plan v1，并已向白金提问测试职责。\n\n{fenced}",
            "bare_tail": f"已阅读 Plan v1，编辑前需澄清测试执行职责。\n\n{raw}",
            "bare_extra_candidate": f'说明 {{"actions": []}}\n{raw}',
            "bare_suffix": f"说明\n{raw}\n已完成",
            "prose_suffix": f"{fenced}\n已完成澄清，请等待回复。",
            "multiple_blocks": f"{fenced}\n{fenced}",
            "prose_only": "已阅读 Plan v1 并已向白金提问测试职责，等待 Plan v2。",
            "extra_candidate": f'{{"actions": []}}\n{fenced}',
            "unknown_field": json.dumps(
                {"actions": [action, {"action": "finish_turn", "success": True}]}
            ),
        }
        self.outputs.append(responses[self.response_style])
        return Process(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "Read", "arguments": {"path": plan["path"]}}}
                    ],
                },
                {
                    "role": "assistant",
                    "content": responses[self.response_style],
                },
            ]
        )


class Boundary:
    grants: ClassVar[list] = []

    def __init__(self, **options):
        self.grants.append(options["read_only_files"])

    def wrap(self, argv):
        return ["sandbox-stub", *argv]


def factory(kimi_runner):
    return lambda worktrees, runtime, policy: KimiCodeAdapter(
        worktree_root=worktrees,
        runtime_root=runtime,
        policy=policy,
        runner=kimi_runner,
        boundary_factory=Boundary,
        env_source={"KIMI_MODEL_API_KEY": "offline-placeholder"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_style", ["raw", "fenced", "prose_prefix", "prose_suffix", "bare_tail"]
)
async def test_plan_clarification_v2_edit_and_verification(tmp_path, response_style):
    planner, kimi = PlannerProcessRunner(), KimiProcessRunner(response_style=response_style)
    Boundary.grants = []
    async with handoff_fixture(tmp_path, CodexCliAdapter(runner=planner), factory(kimi)) as fixture:
        report = await run_handoff(fixture)
        assert report.passed
        assert len(planner.calls) == len(kimi.calls) == 2
        assert len(Boundary.grants) == 2
        assert all(len(paths) == 1 for paths in Boundary.grants)
        assert Boundary.grants[0] != Boundary.grants[1]
        for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER):
            assert fixture.runner.rooms.pending_for(fixture.members[role].member_id) == ()
        messages_to_controller = fixture.runner.rooms.pending_for(
            fixture.members[MemberRole.ORCHESTRATOR].member_id
        )
        assert [item.message.type for item in messages_to_controller] == [
            MessageType.IMPLEMENTATION_READY
        ]
        recorded = fixture.router.trace_store.list(
            trace_id=fixture.task.trace_id, type=TraceEventType.AGENT_OUTPUT_RECORDED
        )
        kimi_outputs = [
            fixture.store.read_json(item.event.payload["artifact_id"])["output"]["message"]
            for item in recorded
            if item.event.payload["role"] == "implementer"
        ]
        assert kimi_outputs == kimi.outputs  # Full prose is retained, not passed as extra actions.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_style", "error"),
    [
        ("multiple_blocks", "not valid JSON"),
        ("extra_candidate", "not valid JSON"),
        ("bare_extra_candidate", "not valid JSON"),
        ("bare_suffix", "not valid JSON"),
        ("prose_only", "not valid JSON"),
        ("unknown_field", "invalid agent chat turn"),
    ],
)
async def test_kimi_invalid_clarification_has_no_ack_or_routing_side_effects(
    tmp_path,
    response_style,
    error,
):
    planner = PlannerProcessRunner()
    kimi = KimiProcessRunner(response_style=response_style)
    async with handoff_fixture(tmp_path, CodexCliAdapter(runner=planner), factory(kimi)) as fixture:
        sender = fixture.members[MemberRole.ORCHESTRATOR]
        fixture.router.route(
            ChatMessage(
                room_id=fixture.room.room_id,
                task_id=fixture.task.id,
                trace_id=fixture.task.trace_id,
                sender_id=sender.member_id,
                recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),),
                type=MessageType.ISSUE_POSTED,
                content=fixture.task.issue,
                idempotency_key="invalid-output-test",
            ),
            authenticated_sender_id=sender.member_id,
        )
        await fixture.turn(MemberRole.PLANNER)
        rooms = fixture.runner.rooms
        room_id = fixture.room.room_id
        messages_before = rooms.list_messages(room_id)
        plans_before = rooms.list_plan_revisions(room_id)
        pending_before = {
            role: rooms.pending_for(member.member_id) for role, member in fixture.members.items()
        }
        assert len(pending_before[MemberRole.IMPLEMENTER]) == 1
        assert pending_before[MemberRole.PLANNER] == ()
        with fixture.store.database.connect() as connection:
            artifacts_before = connection.execute(
                "SELECT artifact_id FROM artifacts ORDER BY artifact_id"
            ).fetchall()
        source_before = (fixture.handle.worktree_path / "src/pricing.py").read_bytes()

        with pytest.raises(ChatActionError, match=error):
            await fixture.turn(MemberRole.IMPLEMENTER)

        # Exercise the production Kimi parser and runner, not just the JSON helper.
        # Neither prose claims nor ambiguous payloads may become a delivered question.
        assert rooms.list_messages(room_id) == messages_before
        assert rooms.list_plan_revisions(room_id) == plans_before
        for role, member in fixture.members.items():
            assert rooms.pending_for(member.member_id) == pending_before[role]
        with fixture.store.database.connect() as connection:
            after = connection.execute(
                "SELECT artifact_id FROM artifacts ORDER BY artifact_id"
            ).fetchall()
        new_ids = {row["artifact_id"] for row in after} - {
            row["artifact_id"] for row in artifacts_before
        }
        assert len(new_ids) == 2  # Diagnostic raw output + received stream, no action Artifact.
        raw_id = next(
            artifact_id for artifact_id in new_ids
            if fixture.store.get_metadata(artifact_id).metadata["purpose"] == "raw-agent-output"
        )
        assert fixture.store.get_metadata(raw_id).metadata["purpose"] == "raw-agent-output"
        assert fixture.store.read_json(raw_id)["output"]["message"] == kimi.outputs[0]
        assert (fixture.handle.worktree_path / "src/pricing.py").read_bytes() == source_before
        assert len(planner.calls) == len(kimi.calls) == 1  # No automatic paid retry.
        assert len(fixture.turns) == 1  # The failed turn is not a completed turn.
        assert fixture.task.state.value == "created"


@pytest.mark.asyncio
async def test_plan_tampering_prevents_kimi_launch_and_preserves_pending_message(tmp_path):
    planner, kimi = PlannerProcessRunner(), KimiProcessRunner()
    async with handoff_fixture(tmp_path, CodexCliAdapter(runner=planner), factory(kimi)) as fixture:
        sender = fixture.members[MemberRole.ORCHESTRATOR]
        fixture.router.route(
            ChatMessage(
                room_id=fixture.room.room_id,
                task_id=fixture.task.id,
                trace_id=fixture.task.trace_id,
                sender_id=sender.member_id,
                recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER),),
                type=MessageType.ISSUE_POSTED,
                content=fixture.task.issue,
                idempotency_key="tamper-test",
            ),
            authenticated_sender_id=sender.member_id,
        )
        plan_turn = await fixture.turn(MemberRole.PLANNER)
        artifact = plan_turn.routed_messages[0].message.artifacts[0]
        fixture.store.blob_path_for(artifact.artifact_id).write_bytes(b"tampered")
        with pytest.raises(AgentTurnError, match="integrity"):
            await fixture.turn(MemberRole.IMPLEMENTER)
        assert kimi.calls == []
        assert (
            len(fixture.runner.rooms.pending_for(fixture.members[MemberRole.IMPLEMENTER].member_id))
            == 1
        )


@pytest.mark.asyncio
async def test_readiness_claim_without_fix_fails_deterministic_verification(tmp_path):
    async with handoff_fixture(
        tmp_path,
        CodexCliAdapter(runner=PlannerProcessRunner()),
        factory(KimiProcessRunner(edit=False)),
    ) as fixture:
        with pytest.raises(AssertionError, match="deterministic checks failed"):
            await run_handoff(fixture)
