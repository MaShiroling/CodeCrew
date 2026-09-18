import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.workspace import (
    CommandExecutionError,
    CommandExecutor,
    CommandPolicy,
    CommandRequest,
    CommandRule,
    CommandStatus,
    WorktreeManager,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


async def make_context(tmp_path: Path, *, max_output_bytes: int = 1024 * 1024):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    script = repository / "runner.py"
    script.write_text(
        "import os, sys, time\n"
        "mode = sys.argv[1]\n"
        "if mode == 'ok': print('ok')\n"
        "elif mode == 'fail': print('bad', file=sys.stderr); raise SystemExit(7)\n"
        "elif mode == 'sleep': time.sleep(10)\n"
        "elif mode == 'large': print('x' * 10000)\n"
        "elif mode == 'env': print(os.environ.get('SAFE_VALUE', 'missing'))\n"
        "elif mode == 'secret': print(os.environ.get('UNRELATED_SECRET', 'missing'))\n"
    )
    git(repository, "add", "runner.py")
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
    policy = CommandPolicy(
        rules=(
            CommandRule(
                name="test-runner",
                argv_prefix=(sys.executable, "runner.py"),
            ),
        ),
        allowed_environment=("SAFE_VALUE",),
        max_timeout_seconds=1,
        max_output_bytes=max_output_bytes,
    )
    return handle, store, CommandExecutor(store, policy)


def request(handle, *arguments: str, **updates: object) -> CommandRequest:
    values: dict[str, object] = {
        "task_id": handle.task_id,
        "trace_id": uuid4(),
        "argv": (sys.executable, "runner.py", *arguments),
    }
    values.update(updates)
    return CommandRequest(**values)


@pytest.mark.asyncio
async def test_allowed_command_records_success_and_output(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path)

    result = await executor.execute(handle, request(handle, "ok"))

    assert result.status is CommandStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.matched_rule == "test-runner"
    assert result.stdout_artifact is not None
    assert store.read_text(result.stdout_artifact.artifact_id) == "ok\n"
    assert result.audit_artifact.type is ArtifactType.COMMAND_AUDIT
    assert store.read_json(result.audit_artifact.artifact_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_nonzero_exit_and_stderr_are_evidence(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path)

    result = await executor.execute(handle, request(handle, "fail"))

    assert result.status is CommandStatus.FAILED
    assert result.exit_code == 7
    assert result.stderr_artifact is not None
    assert store.read_text(result.stderr_artifact.artifact_id) == "bad\n"


@pytest.mark.asyncio
async def test_unlisted_and_shell_like_commands_are_denied_and_audited(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path)
    unlisted = request(handle, "ok").model_copy(update={"argv": ("sh", "-c", "echo bad")})
    shell_token = request(handle, "ok", "&&", "other")

    first = await executor.execute(handle, unlisted)
    second = await executor.execute(handle, shell_token)

    assert first.status is CommandStatus.DENIED
    assert "allowlist" in (first.denial_reason or "")
    assert second.status is CommandStatus.DENIED
    assert "unsafe" in (second.denial_reason or "")
    assert store.read_json(first.audit_artifact.artifact_id)["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["../outside", "/tmp/outside", "--config=/tmp/file"])
async def test_escaping_path_arguments_are_denied(tmp_path: Path, argument: str) -> None:
    handle, _, executor = await make_context(tmp_path)

    result = await executor.execute(handle, request(handle, "ok", argument))

    assert result.status is CommandStatus.DENIED
    assert "unsafe" in (result.denial_reason or "")


@pytest.mark.asyncio
async def test_working_directory_must_exist_inside_allowed_worktree_path(
    tmp_path: Path,
) -> None:
    handle, _, executor = await make_context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (handle.worktree_path / "linked").symlink_to(outside, target_is_directory=True)

    missing = await executor.execute(
        handle, request(handle, "ok", working_directory="missing")
    )
    escaped = await executor.execute(
        handle, request(handle, "ok", working_directory="linked")
    )

    assert missing.status is CommandStatus.DENIED
    assert escaped.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_environment_is_explicitly_allowlisted(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path)

    allowed = await executor.execute(
        handle, request(handle, "env", environment={"SAFE_VALUE": "visible"})
    )
    denied = await executor.execute(
        handle, request(handle, "env", environment={"SECRET_VALUE": "hidden"})
    )

    assert store.read_text(allowed.stdout_artifact.artifact_id) == "visible\n"
    assert denied.status is CommandStatus.DENIED
    audit = store.read_json(allowed.audit_artifact.artifact_id)
    assert audit["environment_names"] == ["SAFE_VALUE"]
    assert "visible" not in str(audit)


@pytest.mark.asyncio
async def test_parent_secrets_are_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    handle, store, executor = await make_context(tmp_path)

    result = await executor.execute(handle, request(handle, "secret"))

    assert result.stdout_artifact is not None
    assert store.read_text(result.stdout_artifact.artifact_id) == "missing\n"


@pytest.mark.asyncio
async def test_shell_is_denied_even_if_a_rule_attempts_to_allow_it(tmp_path: Path) -> None:
    handle, store, _ = await make_context(tmp_path)
    executor = CommandExecutor(
        store,
        CommandPolicy(rules=(CommandRule(name="shell", argv_prefix=("sh", "-c")),)),
    )
    command = CommandRequest(
        task_id=handle.task_id,
        trace_id=uuid4(),
        argv=("sh", "-c", "echo unsafe"),
    )

    result = await executor.execute(handle, command)

    assert result.status is CommandStatus.DENIED
    assert "shell" in (result.denial_reason or "")


@pytest.mark.asyncio
async def test_timeout_kills_process_and_is_audited(tmp_path: Path) -> None:
    handle, _, executor = await make_context(tmp_path)

    result = await executor.execute(
        handle, request(handle, "sleep", timeout_seconds=0.05)
    )

    assert result.status is CommandStatus.TIMED_OUT
    assert result.exit_code is not None


@pytest.mark.asyncio
async def test_output_is_bounded_and_marked_truncated(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path, max_output_bytes=64)

    result = await executor.execute(handle, request(handle, "large"))

    assert result.stdout_truncated
    assert result.stdout_artifact is not None
    assert len(store.read_bytes(result.stdout_artifact.artifact_id)) == 64


@pytest.mark.asyncio
async def test_cancellation_kills_process_and_persists_audit(tmp_path: Path) -> None:
    handle, store, executor = await make_context(tmp_path)
    command = request(handle, "sleep")
    task = asyncio.create_task(executor.execute(handle, command))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with store.database.connect() as connection:
        audits = connection.execute(
            "SELECT artifact_id FROM artifacts WHERE artifact_type = ?",
            (ArtifactType.COMMAND_AUDIT.value,),
        ).fetchall()
    assert audits
    persisted = store.read_json(UUID(audits[-1]["artifact_id"]))
    assert persisted["status"] == "cancelled"


@pytest.mark.asyncio
async def test_request_must_belong_to_worktree_task(tmp_path: Path) -> None:
    handle, _, executor = await make_context(tmp_path)

    with pytest.raises(CommandExecutionError, match="another task"):
        await executor.execute(
            handle, request(handle, "ok").model_copy(update={"task_id": uuid4()})
        )


def test_policy_and_request_validation() -> None:
    with pytest.raises(ValidationError):
        CommandPolicy(rules=())
    with pytest.raises(ValidationError, match="repository-relative"):
        CommandPolicy(
            rules=(CommandRule(name="test", argv_prefix=("test",)),),
            allowed_working_directories=("../outside",),
        )
