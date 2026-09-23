import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.agents import (
    AgentRegistry,
    AgentRole,
    FakeAgentAdapter,
    FakeAgentScenario,
    PermissionMode,
)
from app.api.runtime import build_task_runtime
from app.config import Settings
from app.main import create_app
from app.team import MemberRole
from app.verification import VerificationCheckKind, VerificationCommand, VerificationPlan
from app.workspace import CommandPolicy, CommandRule, PermissionPolicy


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "src/app.py").write_text("value = 1\n")
    (repository / "verify.py").write_text("from src.app import value\nassert value == 2\n")
    for arguments in (
        ("init", "-b", "main"),
        ("add", "."),
        ("-c", "user.name=CodeCrew Tests", "-c", "user.email=tests@codecrew.invalid",
         "commit", "-m", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


class EditingFakeAgentAdapter(FakeAgentAdapter):
    async def start(self, request):
        (request.working_directory / "src/app.py").write_text("value = 2\n")
        return await super().start(request)


def make_runtime(tmp_path: Path, *, edit_code: bool = True):
    registry = AgentRegistry()
    planner = FakeAgentAdapter(
        FakeAgentScenario(
            output={"actions": [
                {"action": "share_plan", "recipient": {"kind": "role", "role": "implementer"},
                 "content": "Change value and verify", "artifact_content": {"steps": ["edit src/app.py"]}},
                {"action": "finish_turn", "content": "Plan ready"},
            ]}
        ),
        name="planner-fake",
    )
    implementer_type = EditingFakeAgentAdapter if edit_code else FakeAgentAdapter
    implementer = implementer_type(
        FakeAgentScenario(
            output={"actions": [
                {"action": "request_review",
                 "recipient": {"kind": "role", "role": "orchestrator"},
                 "content": "Implementation ready"},
                {"action": "finish_turn", "content": "Ready"},
            ]}
        ),
        name="implementer-fake",
    )
    reviewer = FakeAgentAdapter(
        FakeAgentScenario(
            output={"actions": [
                {"action": "approve_review",
                 "recipient": {"kind": "role", "role": "orchestrator"},
                 "content": "Approved", "artifact_content": {"issues": []}},
                {"action": "finish_turn", "content": "Approved"},
            ]}
        ),
        name="reviewer-fake",
    )
    for adapter, role, permission in (
        (planner, AgentRole.PLANNER, PermissionMode.READ_ONLY),
        (implementer, AgentRole.IMPLEMENTER, PermissionMode.WORKSPACE_WRITE),
        (reviewer, AgentRole.REVIEWER, PermissionMode.READ_ONLY),
    ):
        registry.register(adapter, roles={role}, permission_modes={permission})

    verification_plan = VerificationPlan(
        commands=tuple(
            VerificationCommand(
                name=name, kind=kind, argv=(sys.executable, "verify.py")
            )
            for name, kind in (
                ("static", VerificationCheckKind.STATIC_ANALYSIS),
                ("public", VerificationCheckKind.PUBLIC_TESTS),
                ("hidden", VerificationCheckKind.HIDDEN_TESTS),
            )
        )
    )
    runtime = build_task_runtime(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
            artifact_root=tmp_path / "artifacts",
            worktree_root=tmp_path / "worktrees",
        ),
        registry=registry,
        agent_names={
            MemberRole.PLANNER: planner.name,
            MemberRole.IMPLEMENTER: implementer.name,
            MemberRole.REVIEWER: reviewer.name,
        },
        verification_plan=verification_plan,
        permission_policy=PermissionPolicy(allowed_paths=("src",)),
        command_policy=CommandPolicy(
            rules=(CommandRule(name="verify", argv_prefix=(sys.executable, "verify.py")),)
        ),
    )
    return runtime, (planner, implementer, reviewer)


def test_http_task_success_path_includes_trace_and_completion_evidence(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    runtime, agents = make_runtime(tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        created = client.post(
            "/api/v1/tasks", json={"issue": "Set value to two", "repository_path": str(repository)}
        )
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["state"] in {"completed", "failed", "cancelled", "needs_human"}:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("task did not reach a terminal state")

        assert task["state"] == "completed", task
        assert task["revision"] == 2
        assert len(agents[0].requests) == len(agents[1].requests) == len(agents[2].requests) == 1
        task_uuid = UUID(task_id)
        assert (runtime.service.contexts.get(task_uuid).context.worktree.worktree_path
                / "src/app.py").read_text() == "value = 2\n"
        evidence = runtime.recovery.evidence.recover(
            task_id=task_uuid,
            trace_id=runtime.service.tasks.get(task_uuid).task.trace_id,
        )
        assert evidence.verification is not None and evidence.verification.passed
        assert evidence.completion is not None and evidence.completion.passed

        events = client.get(f"/api/v1/tasks/{task_id}/events")
        assert events.status_code == 200
        assert "event: verification_completed" in events.text
        assert "event: completion_decided" in events.text
        assert client.get("/api/v1/tasks", params={"state": "completed"}).json()["items"][0][
            "task_id"
        ] == task_id


def test_http_task_cannot_complete_without_effective_diff(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    runtime, _agents = make_runtime(tmp_path, edit_code=False)
    with TestClient(create_app(runtime=runtime)) as client:
        created = client.post(
            "/api/v1/tasks", json={"issue": "Set value to two", "repository_path": str(repository)}
        )
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["state"] in {"completed", "failed", "cancelled", "needs_human"}:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("task did not reach a terminal state")
        assert task["state"] != "completed"
        assert task["state"] == "needs_human"
