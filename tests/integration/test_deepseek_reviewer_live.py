"""Opt-in, two-turn DeepSeek Reviewer smoke against disposable evidence.

This test spends DeepSeek Platform API quota only when explicitly enabled.
It does not establish OS-level read-only isolation or complete a CodeCrew task.
"""

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.agents import (
    AgentEvent,
    AgentEventType,
    AgentRegistry,
    AgentRequest,
    AgentRole,
    AgentSession,
    DeepSeekClaudeReviewerAdapter,
    PermissionMode,
)
from app.orchestration.reviewer import AgentReviewerRunner
from app.verification import ReviewVerdict
from tests.test_deepseek_reviewer_smoke import make_review_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CODECREW_RUN_DEEPSEEK_REVIEWER_LIVE") != "1",
        reason="set CODECREW_RUN_DEEPSEEK_REVIEWER_LIVE=1 to spend two DeepSeek API turns",
    ),
]


class _RecordingReviewerAdapter(DeepSeekClaudeReviewerAdapter):
    def __init__(self, *, executable: str, env_source: dict[str, str]) -> None:
        super().__init__(executable=executable, env_source=env_source)
        self.started: list[tuple[AgentRequest, AgentSession]] = []

    async def start(self, request: AgentRequest) -> AgentSession:
        session = await super().start(request)
        self.started.append((request, session))
        return session


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


async def _events(adapter: _RecordingReviewerAdapter, session: AgentSession) -> list[AgentEvent]:
    return [event async for event in adapter.stream(session.session_id)]


def _checked_tool_calls(events: list[AgentEvent]) -> set[str]:
    calls = [event.data for event in events if event.type is AgentEventType.TOOL_CALL]
    assert calls, "Reviewer made no observable evidence tool calls"
    names = {str(call.get("name")) for call in calls}
    assert names <= {"Read", "Glob", "Grep"}, "Reviewer attempted an unapproved tool"
    return {
        str(call.get("input", {}).get("file_path") or call.get("input", {}).get("path"))
        for call in calls
        if call.get("name") == "Read" and isinstance(call.get("input"), dict)
    }


async def _run_cases(tmp_path: Path, executable: str, private_home: Path) -> None:
    adapter = _RecordingReviewerAdapter(
        executable=executable,
        env_source={**os.environ, "HOME": str(private_home)},
    )
    preflight = adapter.build_process_env(
        AgentRequest(
            task_id=uuid4(),
            trace_id=uuid4(),
            role=AgentRole.REVIEWER,
            prompt="Environment check; do not start a model turn.",
            working_directory=tmp_path,
        )
    )
    assert preflight["ANTHROPIC_BASE_URL"] == adapter.BASE_URL
    assert preflight["ANTHROPIC_MODEL"] == adapter.MODEL
    assert preflight["HOME"] == str(private_home)
    assert "ANTHROPIC_API_KEY" not in preflight
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in preflight
    registry = AgentRegistry()
    registry.register(
        adapter,
        roles={AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
    )

    native_ids: list[str] = []
    for label, fixed, expected_verdict in (
        ("valid", True, ReviewVerdict.APPROVED),
        ("missing-diff", False, ReviewVerdict.REJECTED),
    ):
        task, handle, store, plan, verification = await make_review_case(
            tmp_path / label, fixed=fixed
        )
        runner = AgentReviewerRunner(
            registry, store, agent_name=adapter.name, timeout_seconds=180
        )
        before_worktree = _snapshot(handle.worktree_path)
        before_artifacts = _snapshot(store.root)

        review = await runner.review(task, handle, plan, verification)
        request, session = adapter.started[-1]
        events = await _events(adapter, session)

        assert review.verdict is expected_verdict
        assert request.resume_from_session_id is None
        assert session.native_session_id is not None
        native_ids.append(session.native_session_id)
        read_paths = _checked_tool_calls(events)
        expected_reads = (plan, verification.artifact, verification.change_set.manifest_artifact)
        if verification.change_set.diff_artifact is not None:
            expected_reads += (verification.change_set.diff_artifact,)
        assert {
            str(store.blob_path_for(artifact.artifact_id)) for artifact in expected_reads
        } <= read_paths, "Reviewer did not visibly read every evidence artifact"
        assert _snapshot(handle.worktree_path) == before_worktree
        assert _snapshot(store.root) == before_artifacts

    assert len(adapter.started) == 2
    assert len(set(native_ids)) == 2


@pytest.mark.asyncio
async def test_live_deepseek_reviewer_approves_evidence_and_rejects_missing_diff(
    tmp_path: Path,
) -> None:
    executable = shutil.which("claude")
    if executable is None:
        pytest.fail("Claude Code CLI is not on PATH")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        pytest.fail("set DEEPSEEK_API_KEY in this terminal; never put it in the command")

    with TemporaryDirectory(prefix="codecrew-deepseek-home-", dir=tmp_path) as home:
        await _run_cases(tmp_path, executable, Path(home))
