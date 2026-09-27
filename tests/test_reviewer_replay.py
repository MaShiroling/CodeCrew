import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from app.agents import AgentCapability, AgentExitReason, AgentResult, AgentRole, PermissionMode
from app.agents.fake import FakeAgentAdapter, FakeAgentScenario
from app.orchestration.models import TaskState
from app.storage import ArtifactType, SQLiteDatabase
from app.team import ChatActionError, MemberRole
from app.trace import TraceActorKind, TraceEvent, TraceEventType
from scripts.replay_reviewer import (
    DEFAULT_FIXTURES,
    main,
    replay_archive,
    replay_fixtures,
    replay_output,
)
from scripts.smoke_evidence import archive_smoke_evidence
from tests.test_agent_turn_runner import make_context, send_trigger

CASES = json.loads(DEFAULT_FIXTURES.read_text(encoding="utf-8"))["cases"]


def snapshot(root):
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def make_archive(tmp_path, *, source="native", failed=False):
    _, router, _, store, _, task, _, _ = make_context(
        tmp_path, FakeAgentScenario()
    )
    session_id = uuid4()
    output = {
        "structured_output": {
            "actions": [
                {
                    "action": "approve_review",
                    "recipient": {"kind": "role", "role": "orchestrator"},
                    "content": "Independent evidence inspected",
                    "artifact_content": {"issues": []},
                },
                {"action": "finish_turn"},
            ]
        }
    }
    if source == "text":
        output = {"result": json.dumps(output["structured_output"])}
    result = AgentResult(
        session_id=session_id,
        trace_id=task.trace_id,
        reason=AgentExitReason.FAILED if failed else AgentExitReason.COMPLETED,
        exit_code=1 if failed else 0,
        output=output,
        duration_ms=1,
    )
    metadata = store.put_json(
        result.model_dump(mode="json"),
        task_id=task.id,
        trace_id=task.trace_id,
        type=ArtifactType.GENERIC,
        created_by="agent-output-recorder",
    )
    router.trace_store.append(
        TraceEvent(
            task_id=task.id,
            trace_id=task.trace_id,
            type=TraceEventType.AGENT_OUTPUT_RECORDED,
            actor_kind=TraceActorKind.DETERMINISTIC,
            actor_id="agent-output-recorder",
            idempotency_key="test-output",
            payload={
                "role": "reviewer",
                "artifact_id": str(metadata.artifact_id),
                "sha256": metadata.sha256,
                "session_id": str(session_id),
            },
        )
    )
    archive = archive_smoke_evidence(store, task, root=tmp_path / "archive")
    return archive


def test_portable_corpus_is_offline_and_expectations_are_explicit(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("replay cannot execute processes or connect to model runtimes")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret-must-not-appear")
    before = DEFAULT_FIXTURES.read_bytes()
    report = replay_fixtures()
    assert report["replay_passed"] and len(report["cases"]) == 6
    assert not report["models_called"] and not report["workflow_executed"]
    assert DEFAULT_FIXTURES.read_bytes() == before
    assert main([]) == 0
    printed = capsys.readouterr().out
    assert "test-only-secret-must-not-appear" not in printed
    assert "task_success" not in printed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_minimized_inputs_are_not_repaired_or_rewritten(case):
    original = deepcopy(case["output"])
    result = replay_output(case["output"], source=case["source"])
    assert all(result.get(key) == value for key, value in case["expected"].items())
    assert case["output"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_corpus_runs_through_production_turn_boundary(tmp_path, case):
    runner, router, rooms, store, _, task, room, members = make_context(
        tmp_path, FakeAgentScenario()
    )
    reviewer = members[MemberRole.REVIEWER]
    adapter = FakeAgentAdapter(
        FakeAgentScenario(output=deepcopy(case["output"])),
        name="replay-reviewer",
        capabilities=frozenset({AgentCapability.CODE_REVIEW}),
    )
    runner.registry.register(
        adapter, roles={AgentRole.REVIEWER}, permission_modes={PermissionMode.READ_ONLY}
    )
    runner.reviewer_structured_output = case["source"] == "native"
    for state in (
        TaskState.PLANNING,
        TaskState.IMPLEMENTING,
        TaskState.VERIFYING,
        TaskState.REVIEWING,
    ):
        task.transition_to(state)
    send_trigger(router, room, members[MemberRole.ORCHESTRATOR], reviewer)
    before_messages = rooms.list_messages(room.room_id)
    if case["expected"]["decision"] == "rejected":
        with pytest.raises(ChatActionError):
            await runner.run(
                task,
                room_id=room.room_id,
                member_id=reviewer.member_id,
                agent_name=adapter.name,
                working_directory=tmp_path,
            )
        assert rooms.pending_for(reviewer.member_id)
        assert rooms.list_messages(room.room_id) == before_messages
        with store.database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE artifact_type='review_report'"
                ).fetchone()[0]
                == 0
            )
    else:
        turn = await runner.run(
            task,
            room_id=room.room_id,
            member_id=reviewer.member_id,
            agent_name=adapter.name,
            working_directory=tmp_path,
        )
        assert turn.routed_messages and not rooms.pending_for(reviewer.member_id)
    assert len(adapter.requests) == 1  # No retry, including rejected output.
    assert task.state is TaskState.REVIEWING and task.rework_rounds == 0
    assert not router.trace_store.list(
        trace_id=task.trace_id, type=TraceEventType.COMPLETION_DECIDED
    )
    recorded = router.trace_store.list(
        trace_id=task.trace_id, type=TraceEventType.AGENT_OUTPUT_RECORDED
    )
    saved = store.read_json(UUID(recorded[-1].event.payload["artifact_id"]))
    assert saved["output"] == case["output"]


@pytest.mark.parametrize("source", ["text", "native"])
def test_archive_replay_is_read_only_and_does_not_resume_task(tmp_path, monkeypatch, source):
    archive = make_archive(tmp_path, source=source)
    before = snapshot(archive)

    def no_normal_database(*args, **kwargs):
        raise AssertionError("Do not use normal WAL-enabled database connections")

    monkeypatch.setattr(SQLiteDatabase, "_open_connection", no_normal_database)
    report = replay_archive(archive, source=source)
    assert report["archive_integrity_verified"] and report["artifact_count"] == 1
    assert report["cases"][0]["decision"] == "accepted"
    assert "task_success" not in report and "replay_passed" not in report
    assert snapshot(archive) == before  # No writes, sidecars, ACKs or Trace appends.


def test_failed_agent_cannot_pass_just_by_returning_valid_json(tmp_path):
    archive = make_archive(tmp_path, failed=True)
    report = replay_archive(archive, source="native")
    assert report["cases"][0]["reason"] == "agent_failed"


@pytest.mark.parametrize(
    "failure",
    [
        "database-hash",
        "blob-hash",
        "missing-blob",
        "blob-symlink",
        "manifest-identity",
        "manifest-incomplete",
        "layout-traversal",
        "nonempty-wal",
        "sidecar-symlink",
    ],
)
def test_invalid_archive_is_rejected_without_changing_input(tmp_path, capsys, failure):
    archive = make_archive(tmp_path)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["artifacts"][0]
    blob = archive / "artifacts/sha256" / record["sha256"][:2] / record["sha256"]
    if failure == "database-hash":
        manifest["database_sha256"] = "0" * 64
    elif failure == "blob-hash":
        blob.write_text("private-corruption-do-not-echo")
    elif failure == "missing-blob":
        blob.unlink()
    elif failure == "blob-symlink":
        target = tmp_path / "external-blob"
        blob.rename(target)
        blob.symlink_to(target)
    elif failure == "manifest-identity":
        record["trace_id"] = str(uuid4())
    elif failure == "manifest-incomplete":
        manifest["artifacts"] = []
    elif failure == "layout-traversal":
        manifest["database"] = "../trace.sqlite3"
    elif failure == "nonempty-wal":
        (archive / "trace.sqlite3-wal").write_bytes(b"uncommitted frames")
    else:
        (archive / "trace.sqlite3-shm").symlink_to(tmp_path / "not-present")
    manifest_path.write_text(json.dumps(manifest))
    before = snapshot(archive)
    assert main(["--archive", str(archive), "--source", "native"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["input_verified"] is False and "private-corruption-do-not-echo" not in str(report)
    assert snapshot(archive) == before


def test_empty_wal_sidecars_are_ignored_without_cleanup(tmp_path):
    archive = make_archive(tmp_path)
    (archive / "trace.sqlite3-wal").write_bytes(b"")
    (archive / "trace.sqlite3-shm").write_bytes(b"existing shared-memory sidecar")
    before = snapshot(archive)
    assert replay_archive(archive, source="native")["archive_integrity_verified"]
    assert snapshot(archive) == before


def test_cli_requires_explicit_archive_source(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--archive", str(tmp_path)])
    assert error.value.code == 2


def test_cli_detects_regression_mismatch_without_printing_output(tmp_path, capsys):
    path = tmp_path / "corpus.json"
    corpus = json.loads(DEFAULT_FIXTURES.read_text())
    corpus["cases"][0]["expected"] = {"decision": "accepted", "reason": "valid_contract"}
    corpus["cases"][0]["output"]["structured_output"]["actions"][0]["content"] = (
        "private-test-prose"
    )
    path.write_text(json.dumps(corpus))
    assert main(["--fixtures", str(path)]) == 1
    printed = capsys.readouterr().out
    assert not json.loads(printed)["replay_passed"] and "private-test-prose" not in printed
