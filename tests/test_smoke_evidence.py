import hashlib
import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.orchestration.models import Task, TaskState
from app.storage import ArtifactIntegrityError, ArtifactStore, ArtifactType, SQLiteDatabase
from scripts.smoke_evidence import archive_smoke_evidence


def fixture_store(root: Path):
    task = Task(issue="Synthetic evidence fixture", repository_path=str(root / "repository"))
    store = ArtifactStore(SQLiteDatabase(root / "trace.sqlite3"), root / "artifacts")
    store.initialize()
    metadata = store.put_json(
        {"result": "original output", "token_usage": None},
        task_id=task.id,
        trace_id=task.trace_id,
        type=ArtifactType.GENERIC,
        created_by="offline-fixture",
    )
    return task, store, metadata


def test_archive_is_independent_private_and_does_not_copy_runtime(tmp_path, monkeypatch):
    source = tmp_path / "source"
    task, store, metadata = fixture_store(source)
    secret = "offline-test-secret"
    monkeypatch.setenv("KIMI_MODEL_API_KEY", secret)
    for folder in ("kimi-runtime", "reviewer-home", "repository", "worktrees"):
        (source / folder).mkdir()
        (source / folder / "private.txt").write_text(secret)
    # Two records sharing one blob must both survive.
    duplicate = store.put_json(
        {"result": "original output", "token_usage": None},
        task_id=task.id, trace_id=task.trace_id,
        type=ArtifactType.GENERIC, created_by="offline-fixture",
    )
    archive = archive_smoke_evidence(store, task, root=tmp_path / "archives")
    second = archive_smoke_evidence(store, task, root=tmp_path / "archives")
    assert archive != second
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["archive_integrity_verified"]
    assert "task_success" not in manifest
    assert manifest["task_state"] == "created"
    assert len(manifest["artifacts"]) == 2
    assert manifest["database_sha256"] == hashlib.sha256(
        (archive / "trace.sqlite3").read_bytes()
    ).hexdigest()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    source.rename(tmp_path / "unavailable-source")
    archived_store = ArtifactStore(SQLiteDatabase(archive / "trace.sqlite3"), archive / "artifacts")
    for artifact_id in (metadata.artifact_id, duplicate.artifact_id):
        assert archived_store.read_json(artifact_id) == {"result": "original output", "token_usage": None}
    for path in archive.rglob("*"):
        assert path.name not in {"private.txt", "kimi-runtime", "reviewer-home", "worktrees"}
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", ["corrupt", "missing", "symlink", "wrong_task"])
def test_invalid_evidence_cannot_produce_verified_manifest(tmp_path, failure):
    task, store, metadata = fixture_store(tmp_path / "source")
    blob = store.blob_path_for(metadata.artifact_id)
    if failure == "corrupt":
        blob.write_text("tampered")
    elif failure == "missing":
        blob.unlink()
    elif failure == "symlink":
        target = tmp_path / "target"
        blob.rename(target)
        blob.symlink_to(target)
    else:
        task = Task(issue="Another task", repository_path=str(tmp_path))
    with pytest.raises((ArtifactIntegrityError, ValueError)):
        archive_smoke_evidence(store, task, root=tmp_path / "archives")
    assert not list((tmp_path / "archives").rglob("manifest.json"))


def test_archive_root_symlink_is_rejected(tmp_path):
    task, store, _ = fixture_store(tmp_path / "source")
    target = tmp_path / "archives"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="root must not be a symlink"):
        archive_smoke_evidence(store, task, root=alias)
    assert list(target.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("planner_timeout", [None, "360"])
@pytest.mark.parametrize("workflow_failed", [False, True])
async def test_live_entry_archive_errors_preserve_failure_and_do_not_accept_success(
    tmp_path, monkeypatch, capsys, workflow_failed, planner_timeout
):
    from tests.integration import test_three_agent_live as live

    task = Task(issue="Offline entry wiring", repository_path=str(tmp_path))
    fixture = SimpleNamespace(task=task, store=None)

    @asynccontextmanager
    async def handoff(*args, **kwargs):
        yield fixture

    async def run(*args, **kwargs):
        assert kwargs == (
            {"planner_timeout_seconds": 360} if planner_timeout is not None else {}
        )
        if workflow_failed:
            raise RuntimeError("synthetic workflow failure")
        task.state = TaskState.COMPLETED  # Stub only; no success evidence is fabricated in production.
        return SimpleNamespace(
            runtime=SimpleNamespace(latest_completion=SimpleNamespace(passed=True)),
            workflow=SimpleNamespace(agent_turns=[None] * 5),
        )

    def bad_archive(*args, **kwargs):
        raise ValueError("sensitive archive detail must not be printed")

    monkeypatch.setenv("KIMI_MODEL_API_KEY", "offline-placeholder")
    if planner_timeout is None:
        monkeypatch.delenv("CODECREW_PLANNER_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CODECREW_PLANNER_TIMEOUT_SECONDS", planner_timeout)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-placeholder")
    monkeypatch.setattr(live.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(live.shutil, "which", lambda name: f"offline/{name}")
    monkeypatch.setattr(live, "CodexCliAdapter", lambda: object())
    monkeypatch.setattr(live, "DeepSeekClaudeReviewerAdapter", lambda **kwargs: object())
    monkeypatch.setattr(live, "handoff_fixture", handoff)
    monkeypatch.setattr(live, "run_three_agent", run)
    monkeypatch.setattr(live, "archive_smoke_evidence", bad_archive)
    with pytest.raises(RuntimeError if workflow_failed else ValueError):
        await live.test_live_three_agent_success_path(tmp_path)
    output = capsys.readouterr().out
    assert json.loads(output)["archive_error"] == "ValueError"
    assert json.loads(output)["archive_integrity_verified"] is False
    assert "sensitive archive detail" not in output
    assert '"task_success": true' not in output
