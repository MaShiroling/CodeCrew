"""Real adapter parsers and runtime wiring, simulated CLI processes only.

No executable/model is invoked. The stub boundary tests wiring, not OS security.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from app import cli
from app.agents import (
    AgentCompatibilityError,
    AgentRole,
    CodexCliAdapter,
    DeepSeekClaudeReviewerAdapter,
    KimiCodeAdapter,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.config import Settings
from app.orchestration.models import Task, TaskState
from app.storage import ArtifactType
from app.team import (
    ChatActionError,
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    RouteNotAllowedError,
    TeamRoom,
    parse_agent_chat_turn,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/server-config.codecrew-team.python.json"


class ProcessStub:
    def __init__(self, payloads):
        self.payloads = payloads

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        for payload in self.payloads:
            yield ProcessChunk(ProcessStream.STDOUT, json.dumps(payload) + "\n")

    async def wait(self):
        return ProcessResult(exit_code=0, duration_ms=1)

    async def cancel(self):
        return None


class RunnerStub:
    def __init__(self, role):
        self.role = role
        self.answer = ""
        self.calls = []
        self.native_id = str(uuid4())

    async def start(self, argv, *, cwd, timeout_seconds, env=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": env})
        if self.role is MemberRole.PLANNER:
            payloads = [
                {"type": "thread.started", "thread_id": self.native_id},
                {"type": "item.completed", "item": {"type": "agent_message", "text": self.answer}},
                {"type": "turn.completed"},
            ]
        elif self.role is MemberRole.IMPLEMENTER:
            payloads = [
                {"role": "meta", "type": "system.version", "version": "fixture"},
                {"role": "assistant", "content": self.answer},
            ]
        else:
            payloads = [
                {"type": "system", "subtype": "init", "session_id": self.native_id},
                {"type": "result", "session_id": self.native_id, "result": self.answer},
            ]
        return ProcessStub(payloads)


class BoundaryStub:
    def __init__(self, **options):
        self.options = options

    def wrap(self, argv):
        return ["sandbox-exec", "-p", "offline-stub", *argv]


@pytest_asyncio.fixture
async def context(tmp_path, monkeypatch):
    runners = {
        role: RunnerStub(role)
        for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)
    }
    # Test placeholders never inherit real credentials from the invoking terminal.
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "offline-kimi-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-deepseek-key")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True if str(path) == "/usr/bin/sandbox-exec" else original_is_file(path),
    )
    monkeypatch.setattr(
        cli,
        "CodexCliAdapter",
        lambda **options: CodexCliAdapter(**options, runner=runners[MemberRole.PLANNER]),
    )
    monkeypatch.setattr(
        cli,
        "KimiCodeAdapter",
        lambda **options: KimiCodeAdapter(
            **options,
            runner=runners[MemberRole.IMPLEMENTER],
            boundary_factory=BoundaryStub,
            env_source={"KIMI_MODEL_API_KEY": "offline-kimi-key", "DEEPSEEK_API_KEY": "wrong"},
        ),
    )
    monkeypatch.setattr(
        cli,
        "DeepSeekClaudeReviewerAdapter",
        lambda **options: DeepSeekClaudeReviewerAdapter(
            **options,
            runner=runners[MemberRole.REVIEWER],
            env_source={"DEEPSEEK_API_KEY": "offline-deepseek-key", "KIMI_MODEL_API_KEY": "wrong"},
        ),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
    )
    service = cli.build_server_app(
        cli.load_server_config(EXAMPLE), settings=settings
    ).state.task_service
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src/example.py").write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init", "-b", "main"),
        ("add", "."),
        (
            "-c",
            "user.name=CodeCrew Fixture",
            "-c",
            "user.email=fixture@codecrew.invalid",
            "commit",
            "-m",
            "fixture",
        ),
    ):
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=20,
        )
    task = Task(
        issue="Offline protocol preflight; no code fix or real review evidence",
        repository_path=str(repository),
    )
    handle = await service.worktrees.create(task_id=task.id, repository=repository)
    room_id = uuid4()
    members = {
        role: RoomMember(
            room_id=room_id,
            role=role,
            name=service.personas.for_role(role).display_name
            if role in runners
            else "orchestrator",
            kind=MemberKind.AGENT if role in runners else MemberKind.SYSTEM,
        )
        for role in (*runners, MemberRole.ORCHESTRATOR)
    }
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Offline trio",
        members=tuple(members.values()),
    )
    service.rooms.create_room(room)
    try:
        yield service, runners, task, room, members, handle
    finally:
        await service.worktrees.remove(task.id)


async def run_turn(context, role, output):
    service, runners, task, room, members, handle = context
    trigger = service.router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=members[MemberRole.ORCHESTRATOR].member_id,
            recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=role),),
            type=MessageType.SYSTEM_EVENT,
            content="Offline chat contract test",
            idempotency_key=f"trigger-{uuid4()}",
        ),
        authenticated_sender_id=members[MemberRole.ORCHESTRATOR].member_id,
    )
    runners[role].answer = output
    result = await service.event_loop.executor.turns.run(
        task,
        room_id=room.room_id,
        member_id=members[role].member_id,
        agent_name=service.agent_names[role],
        working_directory=handle.worktree_path,
    )
    return result, trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "action", "target", "message_type"),
    [
        (MemberRole.PLANNER, "share_plan", "implementer", MessageType.PLAN_SHARED),
        (
            MemberRole.IMPLEMENTER,
            "request_review",
            "orchestrator",
            MessageType.IMPLEMENTATION_READY,
        ),
        (MemberRole.REVIEWER, "approve_review", "orchestrator", MessageType.REVIEW_APPROVED),
        (MemberRole.REVIEWER, "request_rework", "orchestrator", MessageType.REWORK_REQUEST),
    ],
)
async def test_real_adapter_shapes_route_persona_bound_chat_actions(
    context, role, action, target, message_type
):
    service, runners, task, room, members, handle = context
    payload = {
        "action": action,
        "recipient": {"kind": "role", "role": target},
        "content": "Simulated contract output, not success evidence",
    }
    if role is MemberRole.PLANNER:
        payload["artifact_content"] = {"steps": ["edit src/example.py", "verify"]}
    elif role is MemberRole.REVIEWER:
        payload["artifact_content"] = {
            "issues": []
            if action == "approve_review"
            else [
                {
                    "issue_id": str(uuid4()),
                    "priority": "high",
                    "summary": "Missing evidence",
                    "resolved": False,
                }
            ]
        }
    raw = json.dumps({"actions": [payload, {"action": "finish_turn", "content": "Turn ended"}]})
    result, trigger = await run_turn(context, role, f"```json\n{raw}\n```")
    outgoing = result.routed_messages[0].message
    assert outgoing.type is message_type
    assert outgoing.trace_id == task.trace_id
    assert result.consumed_message_ids == (trigger.message.message_id,)
    assert service.rooms.pending_for(members[role].member_id) == ()
    assert task.state is TaskState.CREATED  # No event loop/Verifier/guard executed here.
    assert result.session.role.value == role.value
    assert result.session.native_session_id == (
        None if role is MemberRole.IMPLEMENTER else runners[role].native_id
    )
    call = runners[role].calls[0]
    argv = call["argv"]
    prompt = argv[argv.index("--prompt") + 1] if role is MemberRole.IMPLEMENTER else argv[-1]
    profile = service.personas.for_role(role)
    assert profile.display_name in prompt and profile.l0_self_description in prompt
    assert all(restriction in prompt for restriction in profile.restrictions)
    assert all(principle in prompt for principle in service.personas.team_principles)
    assert "Role action contract:" in prompt
    assert call["cwd"] == handle.worktree_path
    if role is MemberRole.PLANNER:
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert outgoing.artifacts[0].type is ArtifactType.PLAN
        assert service.rooms.latest_plan_revision(room.room_id).version == 1
    elif role is MemberRole.IMPLEMENTER:
        assert argv[:3] == ["sandbox-exec", "-p", "offline-stub"]
        assert "--agent-file" in argv and "--yolo" not in argv
        assert "DEEPSEEK_API_KEY" not in call["env"]
    else:
        assert "--tools=Read,Glob,Grep" in argv
        assert "KIMI_MODEL_API_KEY" not in call["env"]
        assert outgoing.artifacts[0].type is ArtifactType.REVIEW_REPORT
        assert "OMIT artifact_ids" in prompt
        assert "NOT a list of Plan, Diff, verification or log evidence" in prompt
        schema = json.loads(prompt.split("Action schema:\n", 1)[1].split("\n\n", 1)[0])
        from app.team.reviewer_contract import reviewer_turn_schema

        assert schema == reviewer_turn_schema(room.members)
        examples = json.loads(
            prompt.split("Reviewer output examples", 1)[1].split(":\n", 1)[1].split("\n\n", 1)[0]
        )
        assert [parse_agent_chat_turn(example, output_schema=schema).actions[0].action.value for example in examples] == [
            "approve_review", "request_rework"
        ]
        for example in examples:
            assert "artifact_ids" not in example["actions"][0]
            assert set(example["actions"][0]["artifact_content"]) == {"issues"}
        assert examples[1]["actions"][0]["artifact_content"]["issues"][0]["resolved"] is False
    for bound_role, name in service.agent_names.items():
        descriptor = service.event_loop.executor.turns.registry.describe(name)
        assert {item.value for item in descriptor.roles} == {bound_role.value}
        expected = (
            PermissionMode.WORKSPACE_WRITE
            if bound_role is MemberRole.IMPLEMENTER
            else PermissionMode.READ_ONLY
        )
        expected_modes = {expected}
        if bound_role is MemberRole.IMPLEMENTER:
            expected_modes.add(PermissionMode.READ_ONLY)
        assert descriptor.permission_modes == expected_modes
    registry = service.event_loop.executor.turns.registry
    with pytest.raises(AgentCompatibilityError):
        registry.resolve(
            "codex-cli", role=AgentRole.IMPLEMENTER, permission_mode=PermissionMode.WORKSPACE_WRITE
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "payload", "error"),
    [
        (
            MemberRole.REVIEWER,
            {"verdict": "approved", "summary": "legacy", "issues": []},
            ChatActionError,
        ),
        (
            MemberRole.PLANNER,
            {
                "actions": [
                    {
                        "action": "approve_review",
                        "recipient": {"kind": "role", "role": "orchestrator"},
                        "content": "Unauthorized approval",
                        "artifact_content": {"issues": []},
                    },
                    {"action": "finish_turn"},
                ]
            },
            RouteNotAllowedError,
        ),
        (
            MemberRole.REVIEWER,
            {
                "actions": [
                    {
                        "action": "approve_review",
                        "recipient": {"kind": "role", "role": "orchestrator"},
                        "content": "Contradictory approval",
                        "artifact_content": {
                            "issues": [
                                {
                                    "priority": "high",
                                    "summary": "Unresolved defect",
                                    "resolved": False,
                                }
                            ]
                        },
                    },
                    {"action": "finish_turn"},
                ]
            },
            ChatActionError,
        ),
    ],
)
async def test_invalid_or_unauthorized_native_outputs_are_not_acknowledged(
    context, role, payload, error
):
    service, _, task, _room, members, _ = context
    with pytest.raises(error):
        await run_turn(context, role, json.dumps(payload))
    assert len(service.rooms.pending_for(members[role].member_id)) == 1
    assert service.rooms.pending_for(members[MemberRole.ORCHESTRATOR].member_id) == ()
    assert task.state is TaskState.CREATED
