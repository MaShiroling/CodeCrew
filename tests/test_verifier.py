import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.verification import (
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
    VerificationStatus,
    Verifier,
    VerifierError,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    CommandRequest,
    CommandRule,
    CommandStatus,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeManager,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


async def make_context(tmp_path: Path, *, allowed_paths: tuple[str, ...] = ("src",)):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "src").mkdir()
    (repository / "src/app.py").write_text("value = 1\n")
    (repository / "docs").mkdir()
    (repository / "docs/guide.md").write_text("guide\n")
    (repository / "verify.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "name, outcome = sys.argv[1:3]\n"
        "if name == 'mutate': Path('docs/generated.txt').write_text('generated')\n"
        "print(name)\n"
        "raise SystemExit(0 if outcome == 'pass' else 9)\n"
    )
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "initial",
    )
    handle = await WorktreeManager(tmp_path / "worktrees").create(
        task_id=uuid4(), repository=repository
    )
    store = ArtifactStore(
        SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts"
    )
    store.initialize()
    collector = WorkspaceChangeCollector(store)
    permission_gate = PermissionGate(
        store, PermissionPolicy(allowed_paths=allowed_paths)
    )
    executor = CommandExecutor(
        store,
        CommandPolicy(
            rules=(
                CommandRule(
                    name="verifier-script",
                    argv_prefix=(sys.executable, "verify.py"),
                ),
            ),
        ),
    )
    verifier = Verifier(store, collector, permission_gate, executor)
    return handle, store, executor, verifier


def command(name: str, kind: VerificationCheckKind, outcome: str = "pass"):
    return VerificationCommand(
        name=name,
        kind=kind,
        argv=(sys.executable, "verify.py", name, outcome),
    )


def complete_plan(**updates: object) -> VerificationPlan:
    values: dict[str, object] = {
        "commands": (
            command("static", VerificationCheckKind.STATIC_ANALYSIS),
            command("public", VerificationCheckKind.PUBLIC_TESTS),
            command("hidden", VerificationCheckKind.HIDDEN_TESTS),
        )
    }
    values.update(updates)
    return VerificationPlan(**values)


@pytest.mark.asyncio
async def test_complete_valid_evidence_passes_and_is_persisted(tmp_path: Path) -> None:
    handle, store, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    trace_id = uuid4()

    report = await verifier.verify(handle, trace_id=trace_id, plan=complete_plan())

    assert report.passed
    assert all(check.status is VerificationStatus.PASSED for check in report.checks)
    assert len(report.command_results) == 3
    assert report.artifact.type is ArtifactType.VERIFICATION_REPORT
    persisted = store.read_json(report.artifact.artifact_id)
    assert persisted["passed"] is True
    assert len(persisted["command_audit_artifact_ids"]) == 3


@pytest.mark.asyncio
async def test_no_diff_fails_even_when_all_commands_pass(tmp_path: Path) -> None:
    handle, _, _, verifier = await make_context(tmp_path)

    report = await verifier.verify(handle, trace_id=uuid4(), plan=complete_plan())

    assert not report.passed
    diff = next(item for item in report.checks if item.kind is VerificationCheckKind.DIFF)
    assert diff.status is VerificationStatus.FAILED


@pytest.mark.asyncio
async def test_failed_public_test_fails_report(tmp_path: Path) -> None:
    handle, _, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    plan = complete_plan(
        commands=(
            command("static", VerificationCheckKind.STATIC_ANALYSIS),
            command("public", VerificationCheckKind.PUBLIC_TESTS, "fail"),
            command("hidden", VerificationCheckKind.HIDDEN_TESTS),
        )
    )

    report = await verifier.verify(handle, trace_id=uuid4(), plan=plan)

    failed = next(item for item in report.checks if item.name == "public")
    assert not report.passed
    assert failed.status is VerificationStatus.FAILED
    assert report.command_results[1].exit_code == 9


@pytest.mark.asyncio
async def test_missing_required_hidden_tests_is_explicit_failure(tmp_path: Path) -> None:
    handle, _, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    plan = complete_plan(
        commands=(
            command("static", VerificationCheckKind.STATIC_ANALYSIS),
            command("public", VerificationCheckKind.PUBLIC_TESTS),
        )
    )

    report = await verifier.verify(handle, trace_id=uuid4(), plan=plan)

    required = next(item for item in report.checks if item.name == "hidden_tests_required")
    assert required.status is VerificationStatus.FAILED
    assert not report.passed


@pytest.mark.asyncio
async def test_permission_failure_blocks_all_project_commands(tmp_path: Path) -> None:
    handle, _, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "docs/guide.md").write_text("unauthorized\n")

    report = await verifier.verify(handle, trace_id=uuid4(), plan=complete_plan())

    assert not report.permission_report.passed
    assert report.command_results == ()
    command_checks = [item for item in report.checks if item.name in {"static", "public", "hidden"}]
    assert all(item.status is VerificationStatus.BLOCKED for item in command_checks)


@pytest.mark.asyncio
async def test_command_side_effects_are_recollected_and_permission_checked(
    tmp_path: Path,
) -> None:
    handle, _, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    plan = complete_plan(
        commands=(
            command("mutate", VerificationCheckKind.STATIC_ANALYSIS),
            command("public", VerificationCheckKind.PUBLIC_TESTS),
            command("hidden", VerificationCheckKind.HIDDEN_TESTS),
        )
    )

    report = await verifier.verify(handle, trace_id=uuid4(), plan=plan)

    assert not report.passed
    assert "docs/generated.txt" in {
        item.path for item in report.change_set.changed_files
    }
    assert any(
        item.path == "docs/generated.txt"
        for item in report.permission_report.violations
    )


@pytest.mark.asyncio
async def test_denied_verification_command_fails_command_policy(tmp_path: Path) -> None:
    handle, _, _, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    denied_hidden = VerificationCommand(
        name="hidden",
        kind=VerificationCheckKind.HIDDEN_TESTS,
        argv=("sh", "-c", "exit 0"),
    )
    plan = complete_plan(
        commands=(
            command("static", VerificationCheckKind.STATIC_ANALYSIS),
            command("public", VerificationCheckKind.PUBLIC_TESTS),
            denied_hidden,
        )
    )

    report = await verifier.verify(handle, trace_id=uuid4(), plan=plan)

    policy = next(
        item for item in report.checks if item.kind is VerificationCheckKind.COMMAND_POLICY
    )
    assert report.command_results[-1].status is CommandStatus.DENIED
    assert policy.status is VerificationStatus.FAILED
    assert not report.passed


@pytest.mark.asyncio
async def test_prior_denied_implementer_command_fails_verification(tmp_path: Path) -> None:
    handle, _, executor, verifier = await make_context(tmp_path)
    (handle.worktree_path / "src/app.py").write_text("value = 2\n")
    trace_id = uuid4()
    denied = await executor.execute(
        handle,
        CommandRequest(
            task_id=handle.task_id,
            trace_id=trace_id,
            argv=("unapproved-tool",),
        ),
    )

    report = await verifier.verify(
        handle,
        trace_id=trace_id,
        plan=complete_plan(),
        prior_command_results=(denied,),
    )

    assert not report.passed
    assert any(
        item.kind is VerificationCheckKind.COMMAND_POLICY
        and item.status is VerificationStatus.FAILED
        for item in report.checks
    )


@pytest.mark.asyncio
async def test_prior_command_evidence_must_match_task_and_trace(tmp_path: Path) -> None:
    handle, _, executor, verifier = await make_context(tmp_path)
    trace_id = uuid4()
    result = await executor.execute(
        handle,
        CommandRequest(
            task_id=handle.task_id,
            trace_id=trace_id,
            argv=(sys.executable, "verify.py", "precheck", "pass"),
        ),
    )

    with pytest.raises(VerifierError, match="another task"):
        await verifier.verify(
            handle,
            trace_id=trace_id,
            plan=complete_plan(),
            prior_command_results=(result.model_copy(update={"task_id": uuid4()}),),
        )
    with pytest.raises(VerifierError, match="another trace"):
        await verifier.verify(
            handle,
            trace_id=uuid4(),
            plan=complete_plan(),
            prior_command_results=(result,),
        )


def test_verification_command_reuses_safe_command_request_validation() -> None:
    with pytest.raises(ValidationError, match="repository-relative"):
        VerificationCommand(
            name="unsafe",
            kind=VerificationCheckKind.PUBLIC_TESTS,
            argv=("pytest",),
            working_directory="../outside",
        )

    with pytest.raises(ValidationError, match="executable check"):
        VerificationCommand(
            name="invalid-kind",
            kind=VerificationCheckKind.DIFF,
            argv=("pytest",),
        )
