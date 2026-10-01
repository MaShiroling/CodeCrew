"""Disposable, credential-free chat-to-code demonstration.

The Fake implementer intentionally handles one fixed fixture, not arbitrary issues.
"""

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI

from app.agents import AgentRegistry, AgentRole, FakeAgentAdapter, FakeAgentScenario, PermissionMode
from app.api.runtime import build_task_runtime
from app.chat.dispatch import StandaloneChatDispatcher
from app.config import Settings
from app.main import create_app
from app.team import MemberRole
from app.verification import VerificationCheckKind, VerificationCommand, VerificationPlan
from app.workspace import CommandPolicy, CommandRule, PermissionPolicy

DEMO_ISSUE = "只修改 src/app.py，把 value 从 1 改为 2"


class _DemoImplementer(FakeAgentAdapter):
    async def start(self, request):
        # This writes only inside the task's isolated Worktree. It is a fixed
        # scripted demonstration, not an LLM implementing the Human's prose.
        target = request.working_directory / "src/app.py"
        if target.read_text(encoding="utf-8") != "value = 1\n":
            raise ValueError("demo fixture is not at its expected baseline")
        target.write_text("value = 2\n", encoding="utf-8")
        return await super().start(request)


def _create_repository(root: Path) -> Path:
    repository = root / "example-repository"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src/app.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "tests/test_app.py").write_text(
        "import unittest\nfrom src.app import value\n\n"
        "class ValueTest(unittest.TestCase):\n"
        "    def test_value_is_two(self):\n        self.assertEqual(value, 2)\n",
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-b", "main"),
        ("add", "."),
        ("-c", "user.name=CodeCrew Demo", "-c", "user.email=demo@codecrew.invalid",
         "commit", "-m", "Initial demo fixture"),
    ):
        subprocess.run(("git", *arguments), cwd=repository, check=True, capture_output=True)
    return repository


def build_demo_app(root: Path) -> tuple[FastAPI, Path]:
    """Build a new, isolated run; ``root`` must be a fresh empty directory."""
    if not root.is_dir() or any(root.iterdir()):
        raise ValueError("demo root must be a fresh empty directory")
    repository = _create_repository(root)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{root / 'demo.sqlite3'}",
        artifact_root=root / "artifacts",
        worktree_root=root / "worktrees",
        standalone_chat_workspace_root=root / "chat-workspaces",
        standalone_chat_runtime_root=root / "chat-runtime",
    )
    policy = PermissionPolicy(allowed_paths=("src",))
    registry = AgentRegistry()
    planner = FakeAgentAdapter(FakeAgentScenario(output={"actions": [
        {"action": "share_plan", "recipient": {"kind": "role", "role": "implementer"},
         "content": "Only change src/app.py from value = 1 to value = 2",
         "artifact_content": {"steps": ["Change src/app.py", "Run configured checks"]}},
        {"action": "finish_turn", "content": "Demo plan ready"},
    ]}), name="demo-planner")
    implementer = _DemoImplementer(FakeAgentScenario(output={"actions": [
        {"action": "request_review", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Demo edit ready"},
        {"action": "finish_turn", "content": "Demo edit ready"},
    ]}), name="demo-implementer")
    reviewer = FakeAgentAdapter(FakeAgentScenario(output={"actions": [
        {"action": "approve_review", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Demo review approved", "artifact_content": {"issues": []}},
        {"action": "finish_turn", "content": "Demo review approved"},
    ]}), name="demo-reviewer")
    for adapter, role, mode in (
        (planner, AgentRole.PLANNER, PermissionMode.READ_ONLY),
        (implementer, AgentRole.IMPLEMENTER, PermissionMode.WORKSPACE_WRITE),
        (reviewer, AgentRole.REVIEWER, PermissionMode.READ_ONLY),
    ):
        registry.register(adapter, roles={role}, permission_modes={mode})
    checks = (
        ("static", VerificationCheckKind.STATIC_ANALYSIS,
         "compile(open('src/app.py', encoding='utf-8').read(), 'src/app.py', 'exec')"),
        ("public", VerificationCheckKind.PUBLIC_TESTS,
         "import unittest; result=unittest.TestLoader().discover('tests'); assert unittest.TextTestRunner().run(result).wasSuccessful()"),
        ("hidden", VerificationCheckKind.HIDDEN_TESTS,
         "from src.app import value; assert value == 2"),
    )
    commands = tuple((sys.executable, "-B", "-c", script) for _, _, script in checks)
    runtime = build_task_runtime(
        settings=settings, registry=registry,
        agent_names={MemberRole.PLANNER: planner.name,
                     MemberRole.IMPLEMENTER: implementer.name,
                     MemberRole.REVIEWER: reviewer.name},
        verification_plan=VerificationPlan(commands=tuple(
            VerificationCommand(name=name, kind=kind, argv=command)
            for (name, kind, _), command in zip(checks, commands, strict=True)
        )),
        permission_policy=policy,
        command_policy=CommandPolicy(rules=tuple(
            CommandRule(name=f"demo-{index}", argv_prefix=command,
                        allow_extra_args=False)
            for index, command in enumerate(commands)
        )),
    )
    # Keep Fake chat behavior identical to `chat-demo`, but enable a separately
    # confirmed coding task against this generated repository only.
    from app.cli import _build_chat_service, _build_fake_chat_runtime

    chat = _build_chat_service(settings)
    dispatcher = StandaloneChatDispatcher(
        chat.store, _build_fake_chat_runtime(settings), timeout_seconds=30,
    )
    return create_app(
        runtime=runtime, chat_service=chat, chat_dispatcher=dispatcher,
        chat_coding_policy=policy, chat_coding_repository_bound=repository,
        chat_coding_issue_bound=DEMO_ISSUE,
        disable_direct_task_creation=True,
    ), repository
