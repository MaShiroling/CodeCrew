"""Offline handoff using production adapters with simulated CLI processes."""

import json
from typing import ClassVar

import pytest

from app.agents import CodexCliAdapter, KimiCodeAdapter
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.team import AgentTurnError, MemberRole, MessageType
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
    def __init__(self, *, edit=True):
        self.calls = []
        self.edit = edit

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
                    "content": json.dumps(
                        {"actions": [action, {"action": "finish_turn", "content": "Turn ended"}]}
                    ),
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
async def test_plan_clarification_v2_edit_and_verification(tmp_path):
    planner, kimi = PlannerProcessRunner(), KimiProcessRunner()
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


@pytest.mark.asyncio
async def test_plan_tampering_prevents_kimi_launch_and_preserves_pending_message(tmp_path):
    planner, kimi = PlannerProcessRunner(), KimiProcessRunner()
    async with handoff_fixture(tmp_path, CodexCliAdapter(runner=planner), factory(kimi)) as fixture:
        from app.team import ChatMessage, MessageRecipient, RecipientKind

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
