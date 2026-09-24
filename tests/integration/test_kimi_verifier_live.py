"""Opt-in Kimi bug-fix task with independent deterministic verification.

The fixture repository contains only source and public tests. Extra assertions
stay in this harness and are passed to Verifier only after the agent exits.
They are held out from the agent, not a hardened hidden-test service.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import pytest

import app.storage  # noqa: F401 - initialize the existing workspace import graph.
from app.agents import (
    AgentExitReason,
    AgentRequest,
    AgentRole,
    FakeAgentAdapter,
    FakeAgentScenario,
    KimiCodeAdapter,
    PermissionMode,
)
from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase
from app.verification import (
    CompletionConditionKind,
    CompletionGuard,
    ReviewReport,
    ReviewVerdict,
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
    VerificationStatus,
    Verifier,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    CommandRule,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeHandle,
    WorktreeManager,
)

pytestmark = pytest.mark.integration

_BUGGY_CODE = '''"""Split a sequence into consecutive nonempty chunks."""

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> list[list[T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    stop = len(items) - len(items) % size
    return [list(items[start:start + size]) for start in range(0, stop, size)]
'''

_FIXED_CODE = _BUGGY_CODE.replace(
    "stop = len(items) - len(items) % size", "stop = len(items)"
)

_PUBLIC_TESTS = '''from src.chunking import chunked


def test_full_chunks() -> None:
    assert chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_partial_final_chunk_is_preserved() -> None:
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
'''

_STATIC_CHECK = (
    "import ast; from pathlib import Path; "
    "ast.parse(Path('src/chunking.py').read_text(encoding='utf-8'))"
)

_HELD_OUT_CHECK = '''from src.chunking import chunked

assert chunked([1, 2, 3, 4, 5], 3) == [[1, 2, 3], [4, 5]]
assert chunked([1, 2], 5) == [[1, 2]]
assert chunked(("a", "b", "c"), 2) == [["a", "b"], ["c"]]
assert chunked([], 3) == []
for invalid_size in (0, -1):
    try:
        chunked([1], invalid_size)
    except ValueError:
        pass
    else:
        raise AssertionError("nonpositive size must be rejected")
'''

_ISSUE = (
    "Fix src/chunking.py: chunked(items, size) must return consecutive, nonempty "
    "chunks in input order, including a final partial chunk. Empty input returns []. "
    "A nonpositive size raises ValueError. The public tests are in "
    "tests/test_chunking.py. Modify only src/chunking.py; do not edit tests. "
    "You cannot run shell commands in this restricted session. CodeCrew will "
    "run the tests independently after your turn."
)


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=directory, check=True, capture_output=True, text=True, timeout=20
    )


async def _fixture_worktree(tmp_path: Path) -> tuple[WorktreeManager, WorktreeHandle]:
    repository = tmp_path / "buggy-repository"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "src" / "chunking.py").write_text(_BUGGY_CODE, encoding="utf-8")
    (repository / "tests" / "test_chunking.py").write_text(_PUBLIC_TESTS, encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "add", ".")
    _git(
        repository, "-c", "user.name=CodeCrew Fixture", "-c",
        "user.email=fixture@codecrew.invalid", "commit", "-m", "buggy baseline",
    )
    manager = WorktreeManager(tmp_path / "worktrees")
    handle = await manager.create(task_id=uuid4(), repository=repository)
    return manager, handle


def _verification_plan() -> VerificationPlan:
    return VerificationPlan(
        commands=(
            VerificationCommand(
                name="python_syntax",
                kind=VerificationCheckKind.STATIC_ANALYSIS,
                argv=(sys.executable, "-B", "-c", _STATIC_CHECK),
                timeout_seconds=15,
            ),
            VerificationCommand(
                name="public_pytest",
                kind=VerificationCheckKind.PUBLIC_TESTS,
                argv=(
                    sys.executable, "-B", "-m", "pytest", "-q", "-p",
                    "no:cacheprovider", "tests/test_chunking.py",
                ),
                timeout_seconds=30,
            ),
            VerificationCommand(
                name="held_out_assertions",
                kind=VerificationCheckKind.HIDDEN_TESTS,
                argv=(sys.executable, "-B", "-c", _HELD_OUT_CHECK),
                timeout_seconds=15,
            ),
        )
    )


def _verifier(tmp_path: Path) -> tuple[ArtifactStore, Verifier]:
    store = ArtifactStore(SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts")
    store.initialize()
    plan = _verification_plan()
    policy = CommandPolicy(
        rules=tuple(
            CommandRule(
                name=command.name, argv_prefix=command.argv, allow_extra_args=False
            )
            for command in plan.commands
        )
    )
    verifier = Verifier(
        store,
        WorkspaceChangeCollector(store),
        PermissionGate(store, PermissionPolicy(allowed_paths=("src",))),
        CommandExecutor(store, policy),
    )
    return store, verifier


def _synthetic_approval(store: ArtifactStore, task_id: UUID, trace_id: UUID) -> ReviewReport:
    """Offline guard probe only: even an approval cannot override failed tests."""
    content = {
        "task_id": str(task_id),
        "trace_id": str(trace_id),
        "reviewer": "offline-review-probe",
        "verdict": ReviewVerdict.APPROVED.value,
        "issues": [],
    }
    metadata = store.put_json(
        content,
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.REVIEW_REPORT,
        created_by="test",
        filename="synthetic-review.json",
    )
    return ReviewReport(
        task_id=task_id,
        trace_id=trace_id,
        reviewer="offline-review-probe",
        verdict=ReviewVerdict.APPROVED,
        summary="Synthetic approval used only to test the completion guard",
        artifact=ArtifactReference.from_metadata(metadata, summary="Offline guard probe"),
    )


@pytest.mark.asyncio
async def test_bugfix_fixture_fails_before_fix_and_passes_after_fix(tmp_path: Path) -> None:
    manager, handle = await _fixture_worktree(tmp_path)
    try:
        _, verifier = _verifier(tmp_path)
        initial = await verifier.verify(handle, trace_id=uuid4(), plan=_verification_plan())
        assert not initial.passed
        assert {
            check.kind
            for check in initial.checks
            if check.status is VerificationStatus.FAILED
        } >= {
            VerificationCheckKind.DIFF,
            VerificationCheckKind.PUBLIC_TESTS,
            VerificationCheckKind.HIDDEN_TESTS,
        }

        (handle.worktree_path / "src" / "chunking.py").write_text(
            _FIXED_CODE, encoding="utf-8"
        )
        fixed = await verifier.verify(handle, trace_id=uuid4(), plan=_verification_plan())
        assert fixed.passed
        assert [item.path for item in fixed.change_set.changed_files] == ["src/chunking.py"]
        assert fixed.permission_report.passed
    finally:
        await manager.remove(handle.task_id, force=True)


@pytest.mark.asyncio
async def test_agent_self_report_cannot_override_failed_verification(tmp_path: Path) -> None:
    manager, handle = await _fixture_worktree(tmp_path)
    try:
        fake = FakeAgentAdapter(
            FakeAgentScenario(output={"message": "I fixed the bug; all tests pass."})
        )
        trace_id = uuid4()
        session = await fake.start(
            AgentRequest(
                task_id=handle.task_id,
                trace_id=trace_id,
                role=AgentRole.IMPLEMENTER,
                prompt=_ISSUE,
                working_directory=handle.worktree_path,
                permission_mode=PermissionMode.WORKSPACE_WRITE,
            )
        )
        result = await fake.wait(session.session_id)
        assert result.reason is AgentExitReason.COMPLETED
        assert "all tests pass" in result.output["message"]

        store, verifier = _verifier(tmp_path)
        report = await verifier.verify(handle, trace_id=trace_id, plan=_verification_plan())
        decision = CompletionGuard(store).evaluate(
            report, _synthetic_approval(store, handle.task_id, trace_id)
        )
        assert not report.passed
        assert not decision.passed
        assert CompletionConditionKind.VERIFICATION in decision.failed_conditions
        assert CompletionConditionKind.VALID_DIFF in decision.failed_conditions
        assert CompletionConditionKind.REVIEW_APPROVAL not in decision.failed_conditions
    finally:
        await manager.remove(handle.task_id, force=True)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_KIMI_VERIFIER_LIVE") != "1",
    reason="set CODECREW_RUN_KIMI_VERIFIER_LIVE=1 to spend one Kimi Code model turn",
)
async def test_live_kimi_bugfix_requires_independent_verifier(tmp_path: Path) -> None:
    if platform.system() != "Darwin":
        pytest.fail("Kimi live bug-fix test requires macOS Seatbelt")
    if shutil.which("kimi") is None:
        pytest.fail("Kimi CLI is not on PATH")
    if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        pytest.fail("set KIMI_MODEL_API_KEY in this terminal; do not put it in the command")

    manager, handle = await _fixture_worktree(tmp_path)
    try:
        with TemporaryDirectory(prefix="kimi-verifier-", dir=tmp_path) as private_dir:
            adapter = KimiCodeAdapter(
                worktree_root=manager.root,
                runtime_root=Path(private_dir) / "runtime",
                policy=PermissionPolicy(allowed_paths=("src",)),
                max_steps_per_turn=8,
            )
            trace_id = uuid4()
            session = await adapter.start(
                AgentRequest(
                    task_id=handle.task_id,
                    trace_id=trace_id,
                    role=AgentRole.IMPLEMENTER,
                    prompt=_ISSUE,
                    working_directory=handle.worktree_path,
                    permission_mode=PermissionMode.WORKSPACE_WRITE,
                    timeout_seconds=120,
                )
            )
            events = [event async for event in adapter.stream(session.session_id)]
            result = await adapter.wait(session.session_id)

        _, verifier = _verifier(tmp_path)
        report = await verifier.verify(handle, trace_id=trace_id, plan=_verification_plan())
        failed_checks = [
            check.name for check in report.checks
            if check.status is not VerificationStatus.PASSED
        ]
        assert result.reason is AgentExitReason.COMPLETED, (
            f"Kimi turn did not complete: exit={result.exit_code}, "
            f"error_kind={type(result.error).__name__ if result.error else 'none'}"
        )
        assert report.passed, f"deterministic verification failed: {failed_checks}"
        assert [item.path for item in report.change_set.changed_files] == ["src/chunking.py"]
        assert report.permission_report.passed
        assert len(report.command_results) == 3
        assert all(item.exit_code == 0 for item in report.command_results)
        assert events  # The CLI streamed observable execution evidence.
    finally:
        await manager.remove(handle.task_id, force=True)
