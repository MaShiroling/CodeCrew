"""Opt-in P3.5 acceptance: real chat, one Human grant, three real coding roles.

Run only with CODECREW_RUN_CHAT_TO_CODE_LIVE=1. The result is a single observed
task, not a success-rate measurement. No CLI transcript or credential is archived.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.cli import build_server_app
from app.config import Settings
from scripts.chat_to_code_live_fixture import (
    EXPECTED_SOURCE,
    INITIAL_SOURCE,
    ISSUE,
    build_config,
    create_repository,
)

pytestmark = pytest.mark.integration
_TERMINAL_TASKS = {"completed", "failed", "cancelled", "needs_human"}
_TERMINAL_TURNS = {"succeeded", "failed", "cancelled", "interrupted", "budget_exhausted"}


def _preflight(settings: Settings) -> None:
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        pytest.fail("real Kimi implementer requires macOS Seatbelt")
    for env_name, command in (
        ("CODECREW_CODEX_CLI_PATH", settings.codex_cli_path),
        ("CODECREW_KIMI_CLI_PATH", settings.kimi_cli_path),
        ("CODECREW_CLAUDE_CLI_PATH", settings.claude_cli_path),
    ):
        if shutil.which(command) is None:
            pytest.fail(f"CLI unavailable; configure {env_name} in this terminal")
    for name in ("KIMI_MODEL_API_KEY", "DEEPSEEK_API_KEY"):
        if not os.environ.get(name, "").strip():
            pytest.fail(f"set {name} in this terminal; never place it in the command")


def _wait_for_chat(client: TestClient, room_id: str, *, timeout: float) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turns = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"]
        if turns and all(turn["status"] in _TERMINAL_TURNS for turn in turns):
            # Give a just-finished turn time to enqueue its bounded handoff.
            time.sleep(0.3)
            again = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"]
            if len(again) == len(turns) and all(
                turn["status"] in _TERMINAL_TURNS for turn in again
            ):
                return again
        time.sleep(0.5)
    pytest.fail("real chat did not settle before the bounded timeout")


def _wait_for_task(client: TestClient, task_id: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        state = response.json()["state"]
        if state != previous:
            print(json.dumps({"task_id": task_id, "task_state": state}))
            previous = state
        if state in _TERMINAL_TASKS:
            return state
        time.sleep(0.5)
    pytest.fail("real coding task did not settle before the bounded timeout")


def _git_clean(repository: Path) -> bool:
    result = subprocess.run(
        ("git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"),
        capture_output=True, text=True, timeout=10, check=True,
    )
    return not result.stdout.strip()


@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_CHAT_TO_CODE_LIVE") != "1",
    reason="set CODECREW_RUN_CHAT_TO_CODE_LIVE=1 to spend real model turns",
)
def test_real_chat_to_code_small_fix(tmp_path: Path) -> None:
    base = Settings(_env_file=None)
    _preflight(base)
    repository = create_repository(tmp_path)
    settings = base.model_copy(update={
        "database_url": f"sqlite:///{tmp_path / 'task.sqlite3'}",
        "artifact_root": tmp_path / "artifacts",
        "worktree_root": tmp_path / "worktrees",
        "standalone_chat_workspace_root": tmp_path / "chat-workspaces",
        "standalone_chat_runtime_root": tmp_path / "chat-runtime",
        "planner_timeout_seconds": base.planner_timeout_seconds or 360,
    })
    record = {
        "schema_version": 1,
        "scenario": "real-three-agent-chat-to-code-small-fix",
        "status": "incomplete",
        "room_trace_id": None,
        "task_trace_id": None,
        "task_state": None,
        "chat_turn_statuses": [],
        "task_agent_roles": [],
        "trace_event_types": [],
        "trace_failure_types": [],
        "verification_checks": [],
        "review_verdict": None,
        "completion_conditions": [],
        "changed_files": [],
        "patch_sha256": None,
        "patch_bytes": None,
        "source_repository_unchanged": False,
        "limitations": [
            "One observed run is not a reliability benchmark.",
            "Held-out check is not secret from the local operator.",
            "Raw Agent replies, logs and credentials are not archived.",
            "Underlying temporary artifact blobs are deleted after the test.",
        ],
    }
    try:
        app = build_server_app(
            build_config(), settings=settings, repository_bound=repository,
            issue_bound=ISSUE, disable_direct_task_creation=True,
            reviewer_home=tmp_path / "reviewer-home",
        )
        with TestClient(app) as client:
            room_response = client.post("/api/v1/chats", json={
                "title": "真实三 Agent 小修复验收", "idempotency_key": str(uuid4()),
            })
            assert room_response.status_code == 201
            room = room_response.json()
            room_id = room["room_id"]
            record["room_trace_id"] = room["trace_id"]
            message_response = client.post(f"/api/v1/chats/{room_id}/messages", json={
                "content": (
                    "@白金 请先只读讨论一个很小的修复：src/app.py 的 value 要从 1 改为 2。"
                    "只回复 Human，不邀请队友；此消息不授权修改代码。"
                ),
                "idempotency_key": str(uuid4()),
            })
            assert message_response.status_code == 201
            source_id = message_response.json()["message"]["message"]["message_id"]
            turns = _wait_for_chat(client, room_id, timeout=900)
            record["chat_turn_statuses"] = [turn["status"] for turn in turns]
            assert turns and all(turn["status"] == "succeeded" for turn in turns)
            assert client.get("/api/v1/tasks").json()["items"] == []
            assert (repository / "src/app.py").read_text(encoding="utf-8") == INITIAL_SOURCE

            draft = {
                "source_message_id": source_id,
                "repository_path": str(repository),
                "issue": ISSUE,
                "allowed_paths": ["src"],
            }
            preview = client.post(f"/api/v1/chats/{room_id}/coding-task-preflight",
                                  json=draft)
            assert preview.status_code == 200
            assert preview.json()["execution_authorized"] is False
            assert preview.json()["task_created"] is False
            assert client.get("/api/v1/tasks").json()["items"] == []
            created = client.post(f"/api/v1/chats/{room_id}/coding-tasks", json={
                **draft,
                "expected_base_commit": preview.json()["base_commit"],
                "idempotency_key": str(uuid4()),
                "confirmation": "authorize_one_coding_task",
            })
            assert created.status_code == 201
            task_id = created.json()["task_id"]
            record["task_trace_id"] = created.json()["task_trace_id"]
            state = _wait_for_task(client, task_id, timeout=1800)
            record["task_state"] = state
            room_messages = client.get(
                f"/api/v1/tasks/{task_id}/messages", params={"limit": 100}
            )
            assert room_messages.status_code == 200
            record["task_agent_roles"] = sorted({
                item["sender_role"] for item in room_messages.json()["items"]
                if item["sender_role"] in {"planner", "implementer", "reviewer"}
            })
            trace = app.state.task_service.router.trace_store.list(
                trace_id=UUID(record["task_trace_id"]), limit=1000,
            )
            record["trace_event_types"] = [item.event.type.value for item in trace]
            record["trace_failure_types"] = [
                {"event": item.event.type.value,
                 "error_type": item.event.payload["error_type"]}
                for item in trace if "error_type" in item.event.payload
            ]
            delivery_response = client.get(f"/api/v1/tasks/{task_id}/delivery")
            assert delivery_response.status_code == 200
            delivery = delivery_response.json()
            verification = delivery.get("verification") or {}
            review = delivery.get("review") or {}
            completion = delivery.get("completion") or {}
            record["verification_checks"] = [
                {"kind": item["kind"], "status": item["status"]}
                for item in verification.get("checks", [])
            ]
            record["changed_files"] = verification.get("changed_files", [])
            record["review_verdict"] = review.get("verdict")
            record["completion_conditions"] = [
                {"kind": item["kind"], "passed": item["passed"]}
                for item in completion.get("conditions", [])
            ]
            record["evidence_artifact_ids"] = {
                kind: item["artifact"]["artifact_id"]
                for kind, item in (("verification", verification), ("review", review),
                                   ("completion", completion)) if item.get("artifact")
            }
            assert state == "completed"
            assert record["task_agent_roles"] == ["implementer", "planner", "reviewer"]
            assert delivery["delivery_ready"] is True
            assert verification["passed"] is True
            assert set(record["changed_files"]) == {"src/app.py"}
            assert all(check["status"] == "passed" for check in record["verification_checks"])
            assert review["verdict"] == "approved"
            assert review["follows_latest_verification"] is True
            assert completion["passed"] is True
            assert all(condition["passed"] for condition in record["completion_conditions"])
            worktree = app.state.task_service.contexts.get(
                UUID(task_id)
            ).context.worktree.worktree_path
            assert (worktree / "src/app.py").read_text(encoding="utf-8") == EXPECTED_SOURCE
            patch = delivery["patch"]
            response = client.get(f"/api/v1/tasks/{task_id}/delivery/patch/{patch['artifact_id']}")
            assert response.status_code == 200
            assert b"-value = 1" in response.content
            assert b"+value = 2" in response.content
            record["patch_sha256"] = hashlib.sha256(response.content).hexdigest()
            record["patch_bytes"] = len(response.content)
            record["source_repository_unchanged"] = (
                _git_clean(repository)
                and (repository / "src/app.py").read_text(encoding="utf-8") == INITIAL_SOURCE
            )
            assert record["source_repository_unchanged"] is True
            record["status"] = "accepted"
    finally:
        record["source_repository_unchanged"] = (
            _git_clean(repository)
            and (repository / "src/app.py").read_text(encoding="utf-8") == INITIAL_SOURCE
        )
        root = Path(__file__).resolve().parents[2] / "evals/results/chat-to-code-live"
        root.mkdir(parents=True, exist_ok=True)
        filename = record["task_trace_id"] or record["room_trace_id"] or str(uuid4())
        evidence = root / f"{filename}.json"
        evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence.chmod(0o600)
        print(json.dumps({
            "evidence": str(evidence), "status": record["status"],
            "task_trace_id": record["task_trace_id"],
            "task_state": record["task_state"],
        }, ensure_ascii=False))
