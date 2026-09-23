from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents import AgentRegistry, AgentRole, FakeAgentAdapter, PermissionMode
from app.api.runtime import build_task_runtime
from app.config import Settings
from app.main import create_app
from app.team import MemberRole
from app.verification import VerificationCheckKind, VerificationCommand, VerificationPlan
from app.workspace import CommandPolicy, CommandRule, PermissionPolicy


def make_registry() -> tuple[AgentRegistry, dict[MemberRole, str]]:
    registry = AgentRegistry()
    names = {
        MemberRole.PLANNER: "planner",
        MemberRole.IMPLEMENTER: "implementer",
        MemberRole.REVIEWER: "reviewer",
    }
    for member_role, agent_role, permission in (
        (MemberRole.PLANNER, AgentRole.PLANNER, PermissionMode.READ_ONLY),
        (MemberRole.IMPLEMENTER, AgentRole.IMPLEMENTER, PermissionMode.WORKSPACE_WRITE),
        (MemberRole.REVIEWER, AgentRole.REVIEWER, PermissionMode.READ_ONLY),
    ):
        registry.register(
            FakeAgentAdapter(name=names[member_role]),
            roles={agent_role},
            permission_modes={permission},
        )
    return registry, names


def plan() -> VerificationPlan:
    return VerificationPlan(
        commands=tuple(
            VerificationCommand(name=name, kind=kind, argv=("python", "-m", "pytest"))
            for name, kind in (
                ("static", VerificationCheckKind.STATIC_ANALYSIS),
                ("public", VerificationCheckKind.PUBLIC_TESTS),
                ("hidden", VerificationCheckKind.HIDDEN_TESTS),
            )
        )
    )


def test_configured_app_lifespan_initializes_task_api(tmp_path: Path) -> None:
    registry, names = make_registry()
    runtime = build_task_runtime(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
            artifact_root=tmp_path / "artifacts",
            worktree_root=tmp_path / "worktrees",
        ),
        registry=registry,
        agent_names=names,
        verification_plan=plan(),
        permission_policy=PermissionPolicy(allowed_paths=("src",)),
        command_policy=CommandPolicy(
            rules=(CommandRule(name="pytest", argv_prefix=("python", "-m", "pytest")),)
        ),
    )
    with TestClient(create_app(runtime=runtime)) as client:
        response = client.get("/api/v1/tasks")
        assert response.status_code == 200
        assert response.json()["items"] == []


def test_runtime_rejects_missing_hidden_check(tmp_path: Path) -> None:
    registry, names = make_registry()
    incomplete = VerificationPlan(
        commands=tuple(
            command for command in plan().commands
            if command.kind is not VerificationCheckKind.HIDDEN_TESTS
        )
    )
    with pytest.raises(ValueError, match="hidden"):
        build_task_runtime(
            settings=Settings(database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}"),
            registry=registry,
            agent_names=names,
            verification_plan=incomplete,
            permission_policy=PermissionPolicy(allowed_paths=("src",)),
            command_policy=CommandPolicy(
                rules=(CommandRule(name="pytest", argv_prefix=("python", "-m", "pytest")),)
            ),
        )
