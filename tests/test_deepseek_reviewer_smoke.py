"""Offline reviewer smoke using real evidence and a simulated CLI process.

No test in this module invokes Claude Code or makes a model API request.
"""

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agents import (
    AgentRegistry,
    AgentRole,
    DeepSeekClaudeReviewerAdapter,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.orchestration.models import Task
from app.orchestration.reviewer import AgentReviewerRunner, ReviewerExecutionError
from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase
from app.verification import (
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
    WorktreeManager,
)

_BUGGY_SOURCE = "def total(items: list[int]) -> int:\n    return sum(items[:-1])\n"
_FIXED_SOURCE = "def total(items: list[int]) -> int:\n    return sum(items)\n"
_PUBLIC_TESTS = (
    "from src.pricing import total\n\n"
    "def test_multiple_items():\n    assert total([1, 2, 3]) == 6\n\n"
    "def test_empty_items():\n    assert total([]) == 0\n"
)
_HELD_OUT_CHECK = (
    "from src.pricing import total; "
    "assert total([-2, 3]) == 1; assert total([5]) == 5"
)


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, timeout=20
    )


async def _make_fixture(tmp_path: Path):
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src/__init__.py").write_text("", encoding="utf-8")
    (repository / "src/pricing.py").write_text(_BUGGY_SOURCE, encoding="utf-8")
    (repository / "tests/test_pricing.py").write_text(_PUBLIC_TESTS, encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "add", ".")
    _git(
        repository, "-c", "user.name=CodeCrew Fixture", "-c",
        "user.email=fixture@codecrew.invalid", "commit", "-m", "buggy baseline",
    )
    manager = WorktreeManager(tmp_path / "worktrees")
    handle = await manager.create(task_id=uuid4(), repository=repository)
    store = ArtifactStore(SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts")
    store.initialize()
    commands = (
        VerificationCommand(
            name="syntax",
            kind=VerificationCheckKind.STATIC_ANALYSIS,
            argv=(
                sys.executable, "-B", "-c",
                (
                    "import ast; from pathlib import Path; "
                    "ast.parse(Path('src/pricing.py').read_text(encoding='utf-8'))"
                ),
            ),
        ),
        VerificationCommand(
            name="public",
            kind=VerificationCheckKind.PUBLIC_TESTS,
            argv=(
                sys.executable, "-B", "-m", "pytest", "-q", "-p",
                "no:cacheprovider", "tests/test_pricing.py",
            ),
        ),
        VerificationCommand(
            name="held_out",
            kind=VerificationCheckKind.HIDDEN_TESTS,
            argv=(sys.executable, "-B", "-c", _HELD_OUT_CHECK),
        ),
    )
    verifier = Verifier(
        store,
        WorkspaceChangeCollector(store),
        PermissionGate(store, PermissionPolicy(allowed_paths=("src",))),
        CommandExecutor(
            store,
            CommandPolicy(
                rules=tuple(
                    CommandRule(name=command.name, argv_prefix=command.argv)
                    for command in commands
                )
            ),
        ),
    )
    return handle, store, verifier, VerificationPlan(commands=commands)


class _CompletedProcess:
    def __init__(self, payload: dict[str, Any], exit_code: int = 0) -> None:
        native_session_id = str(uuid4())
        self.chunks = (
            ProcessChunk(
                ProcessStream.STDOUT,
                json.dumps(
                    {"type": "system", "subtype": "init", "session_id": native_session_id}
                ) + "\n",
            ),
            ProcessChunk(
                ProcessStream.STDOUT,
                json.dumps({"type": "result", "session_id": native_session_id, **payload})
                + "\n",
            ),
        )
        self.result = ProcessResult(exit_code=exit_code, duration_ms=1)

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        for chunk in self.chunks:
            yield chunk

    async def wait(self) -> ProcessResult:
        return self.result

    async def cancel(self) -> None:
        return None


class _QueuedProcessRunner:
    def __init__(self, *responses: tuple[dict[str, Any], int]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> _CompletedProcess:
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "env": env})
        payload, exit_code = self.responses.pop(0)
        return _CompletedProcess(payload, exit_code)


async def make_review_case(tmp_path: Path, *, fixed: bool = True):
    """Create a disposable worktree with real verifier artifacts.

    The unfixed variant has no effective Diff and failing public/held-out checks.
    """
    handle, store, verifier, verification_plan = await _make_fixture(tmp_path)
    if fixed:
        (handle.worktree_path / "src/pricing.py").write_text(_FIXED_SOURCE, encoding="utf-8")
    trace_id = uuid4()
    verification = await verifier.verify(handle, trace_id=trace_id, plan=verification_plan)
    assert verification.passed is fixed
    assert (verification.change_set.diff_artifact is not None) is fixed
    plan_metadata = store.put_json(
        {"steps": ["Include every input item in src/pricing.py total()"], "allowed_paths": ["src"]},
        task_id=handle.task_id,
        trace_id=trace_id,
        type=ArtifactType.PLAN,
        created_by="offline-planner-fixture",
        filename="plan.json",
    )
    plan = ArtifactReference.from_metadata(plan_metadata, summary="Fixed reviewer plan")
    task = Task(
        id=handle.task_id,
        trace_id=trace_id,
        issue="Fix src/pricing.py total(): include every input item; do not edit tests.",
        repository_path=str(handle.repository_root),
    )
    return task, handle, store, plan, verification


def make_reviewer(store, process_runner: _QueuedProcessRunner):
    adapter = DeepSeekClaudeReviewerAdapter(
        runner=process_runner,
        env_source={"DEEPSEEK_API_KEY": "offline-fixture-key", "PATH": "/usr/bin"},
    )
    registry = AgentRegistry()
    registry.register(
        adapter,
        roles={AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    return AgentReviewerRunner(registry, store, agent_name=adapter.name), adapter


def _response(result: str, *, is_error: bool = False) -> tuple[dict[str, Any], int]:
    return {"result": result, "is_error": is_error}, 0


@pytest.mark.asyncio
async def test_review_fixture_baseline_really_fails(tmp_path: Path) -> None:
    _, _, _, _, report = await make_review_case(tmp_path, fixed=False)

    assert not report.passed
    assert {
        check.kind for check in report.checks if check.status is VerificationStatus.FAILED
    } >= {
        VerificationCheckKind.DIFF,
        VerificationCheckKind.PUBLIC_TESTS,
        VerificationCheckKind.HIDDEN_TESTS,
    }


@pytest.mark.asyncio
async def test_fixed_evidence_reaches_two_independent_reviewer_sessions(tmp_path: Path) -> None:
    task, handle, store, plan, verification = await make_review_case(tmp_path)
    approved = _response('{"verdict":"approved","summary":"Diff matches issue","issues":[]}')
    rejected = _response(
        '{"verdict":"rejected","summary":"Needs more evidence","issues":'
        '[{"priority":"high","summary":"Check missing case","resolved":false}]}'
    )
    process_runner = _QueuedProcessRunner(approved, rejected)
    reviewer, adapter = make_reviewer(store, process_runner)
    before = (handle.worktree_path / "src/pricing.py").read_bytes()

    first = await reviewer.review(task, handle, plan, verification)
    second = await reviewer.review(task, handle, plan, verification)

    assert first.verdict is ReviewVerdict.APPROVED
    assert second.verdict is ReviewVerdict.REJECTED
    assert second.issues[0].summary == "Check missing case"
    assert len(process_runner.calls) == 2
    assert len(adapter._sessions) == 2
    assert len({state.session.native_session_id for state in adapter._sessions.values()}) == 2
    assert (handle.worktree_path / "src/pricing.py").read_bytes() == before
    for call in process_runner.calls:
        assert call["cwd"] == handle.worktree_path
        assert "--resume" not in call["argv"]
        assert "--tools=Read,Glob,Grep" in call["argv"]
        assert call["env"]["ANTHROPIC_AUTH_TOKEN"] == "offline-fixture-key"
        prompt = call["argv"][-1]
        assert task.issue in prompt
        for artifact in (
            plan,
            verification.artifact,
            verification.change_set.manifest_artifact,
            verification.change_set.diff_artifact,
        ):
            path = store.blob_path_for(artifact.artifact_id)
            assert path.is_file()
            assert str(path) in prompt
    assert store.read_json(verification.artifact.artifact_id)["passed"] is True
    assert [item.path for item in verification.change_set.changed_files] == ["src/pricing.py"]
    assert {
        check.kind
        for check in verification.checks
        if check.status is VerificationStatus.PASSED
    } >= {
        VerificationCheckKind.STATIC_ANALYSIS,
        VerificationCheckKind.PUBLIC_TESTS,
        VerificationCheckKind.HIDDEN_TESTS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _response("not JSON"),
        _response('{"verdict":"approved","summary":"ok","issues":[],"extra":true}'),
        _response('{"verdict":"uncertain","summary":"ok","issues":[]}'),
        _response(
            '{"verdict":"approved","summary":"ok","issues":'
            '[{"priority":"urgent","summary":"bad","resolved":false}]}'
        ),
        _response("provider unavailable", is_error=True),
        ({"result": "unexpected exit"}, 1),
    ],
)
async def test_bad_or_failed_reviewer_result_never_becomes_approval(
    tmp_path: Path, response: tuple[dict[str, Any], int]
) -> None:
    task, handle, store, plan, verification = await make_review_case(tmp_path)
    reviewer, _ = make_reviewer(store, _QueuedProcessRunner(response))

    with pytest.raises(ReviewerExecutionError):
        await reviewer.review(task, handle, plan, verification)
