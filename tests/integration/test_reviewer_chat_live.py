"""Explicit opt-in: Reviewer native chat only, not the old verdict protocol."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.agents import AgentRequest, AgentRole, DeepSeekClaudeReviewerAdapter
from app.team.reviewer_contract import reviewer_turn_schema
from app.trace import TraceActorKind, TraceEvent, TraceEventType
from scripts.reviewer_chat_smoke import reviewer_chat_fixture, run_reviewer_chat
from scripts.smoke_evidence import archive_smoke_evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CODECREW_RUN_REVIEWER_CHAT_LIVE") != "1",
        reason="enable CODECREW_RUN_REVIEWER_CHAT_LIVE=1 to spend only DeepSeek Reviewer turns",
    ),
]


def _preflight(executable: str, root: Path):
    result = subprocess.run(
        [executable, "--help"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("Claude CLI help preflight failed")
    request = AgentRequest(
        task_id=uuid4(),
        trace_id=uuid4(),
        role=AgentRole.REVIEWER,
        prompt="Local compatibility check only",
        working_directory=root,
        output_schema=reviewer_turn_schema(),
    )
    argv = DeepSeekClaudeReviewerAdapter(executable=executable).build_command(request)
    for option in {arg.split("=", 1)[0] for arg in argv if arg.startswith("--")}:
        if option not in result.stdout:
            raise RuntimeError("Claude CLI lacks required Reviewer native options")


async def run_live_reviewer_chat(tmp_path, *, scenario):
    # Explicit shell environment only. No Settings/.env, Codex/Kimi login or key.
    executable = shutil.which("claude")
    if executable is None:
        pytest.fail("Claude Code CLI is not on PATH")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        pytest.fail("set DEEPSEEK_API_KEY in this terminal; never put it in the command")
    _preflight(executable, tmp_path)
    with TemporaryDirectory(prefix="reviewer-chat-home-", dir=tmp_path) as private_home:
        reviewer = DeepSeekClaudeReviewerAdapter(
            executable=executable, env_source={**os.environ, "HOME": private_home}
        )
        async with reviewer_chat_fixture(tmp_path, reviewer) as fixture:
            try:
                report = await run_reviewer_chat(fixture, scenario=scenario)
            except Exception as error:
                fixture.router.trace_store.append(
                    TraceEvent(
                        task_id=fixture.task.id,
                        trace_id=fixture.task.trace_id,
                        type=TraceEventType.SYSTEM_ERROR,
                        actor_kind=TraceActorKind.SYSTEM,
                        actor_id="reviewer-chat-smoke",
                        idempotency_key=f"reviewer-smoke-failed:{uuid4()}",
                        payload={
                            "scope": "reviewer-chat-acceptance",
                            "scenario": scenario,
                            "error_type": type(error).__name__,
                        },
                    )
                )
                raise
            finally:
                original_error = sys.exception()
                try:
                    archive = archive_smoke_evidence(
                        fixture.store,
                        fixture.task,
                        root=Path(__file__).resolve().parents[2]
                        / "evals/results"
                        / f"reviewer-chat-{scenario}-live",
                    )
                except Exception as archive_error:
                    print(
                        json.dumps(
                            {
                                "trace_id": str(fixture.task.trace_id),
                                "archive_error": type(archive_error).__name__,
                                "archive_integrity_verified": False,
                            }
                        )
                    )
                    if original_error is None:
                        raise
                else:
                    print(
                        json.dumps(
                            {
                                "trace_id": str(fixture.task.trace_id),
                                "scenario": scenario,
                                "evidence_archive": str(archive),
                                "archive_integrity_verified": True,
                            }
                        )
                    )
            print(
                json.dumps(
                    {
                        "trace_id": str(fixture.task.trace_id),
                        "scenario": scenario,
                        "reviewer_acceptance_passed": True,
                        "report_artifact_id": str(report.artifact_id),
                        "reviewer_turns": len(fixture.turns),
                        "task_completion_evaluated": False,
                    }
                )
            )


@pytest.mark.asyncio
async def test_live_reviewer_native_approval(tmp_path):
    await run_live_reviewer_chat(tmp_path, scenario="approval")


@pytest.mark.asyncio
async def test_live_reviewer_native_rework_then_approval(tmp_path):
    await run_live_reviewer_chat(tmp_path, scenario="rework")
