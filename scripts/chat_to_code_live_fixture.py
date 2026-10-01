"""Disposable, fixed real-model acceptance fixture for P3.5.

The checks are deterministic but the held-out assertion is not a secret from
the local operator. This is one acceptance task, not a reliability benchmark.
"""

import subprocess
import sys
from pathlib import Path

from app.cli import ServerConfig
from app.verification import VerificationCheckKind, VerificationCommand, VerificationPlan
from app.workspace import CommandPolicy, CommandRule, PermissionPolicy

ISSUE = (
    "In this small example repository, change only src/app.py so its value is 2 instead "
    "of 1. Preserve the variable name and do not edit tests or create other files. "
    "Planner: provide a structured plan. Implementer: edit only src/app.py and request "
    "review; do not run shell commands. CodeCrew's deterministic Verifier runs all "
    "checks. Reviewer: inspect the issue, plan, diff and verification evidence before "
    "approving. No Agent may declare the task successful."
)
INITIAL_SOURCE = "value = 1\n"
EXPECTED_SOURCE = "value = 2\n"


def create_repository(root: Path) -> Path:
    """Create a fresh, committed Git fixture; never reuse a user repository."""
    repository = root / "repository"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src/app.py").write_text(INITIAL_SOURCE, encoding="utf-8")
    (repository / "tests/test_app.py").write_text(
        "from src.app import value\n\n"
        "def test_value_is_two():\n    assert value == 2\n",
        encoding="utf-8",
    )
    for args in (
        ("init", "-b", "main"),
        ("add", "."),
        ("-c", "user.name=CodeCrew Live", "-c", "user.email=live@codecrew.invalid",
         "commit", "-m", "Initial live fixture"),
    ):
        subprocess.run(("git", *args), cwd=repository, check=True,
                       capture_output=True, timeout=20)
    return repository


def build_config() -> ServerConfig:
    commands = (
        VerificationCommand(
            name="syntax", kind=VerificationCheckKind.STATIC_ANALYSIS,
            argv=(sys.executable, "-B", "-c",
                  "import ast; from pathlib import Path; ast.parse(Path('src/app.py').read_text())"),
        ),
        VerificationCommand(
            name="public", kind=VerificationCheckKind.PUBLIC_TESTS,
            argv=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  "tests/test_app.py"),
        ),
        VerificationCommand(
            name="held-out", kind=VerificationCheckKind.HIDDEN_TESTS,
            argv=(sys.executable, "-B", "-c",
                  "from src.app import value; assert value == 2"),
        ),
    )
    return ServerConfig(
        planner_adapter="codex-cli",
        implementer_adapter="kimi-code-cli",
        reviewer_adapter="deepseek-claude-reviewer",
        verification_plan=VerificationPlan(commands=commands),
        permission_policy=PermissionPolicy(allowed_paths=("src",)),
        command_policy=CommandPolicy(rules=tuple(
            CommandRule(name=f"acceptance-{command.name}", argv_prefix=command.argv,
                        allow_extra_args=False)
            for command in commands
        )),
    )
