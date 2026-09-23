from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.recovery import EvidenceRecoveryError, EvidenceRecoveryService
from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase
from app.trace import TraceActorKind, TraceEvent, TraceEventType, TraceStore


def _put_json(
    artifacts: ArtifactStore,
    content: dict,
    *,
    task_id: UUID,
    trace_id: UUID,
    type: ArtifactType,
    filename: str,
) -> ArtifactReference:
    metadata = artifacts.put_json(
        content,
        task_id=task_id,
        trace_id=trace_id,
        type=type,
        created_by="test",
        filename=filename,
    )
    return ArtifactReference.from_metadata(metadata, summary=filename)


def _evidence_bundle(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "codecrew.db")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    traces = TraceStore(database)
    artifacts.initialize()
    traces.initialize()
    task_id = uuid4()
    trace_id = uuid4()
    revision = "a" * 40

    diff = artifacts.put_text(
        "diff --git a/app.py b/app.py\n",
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.DIFF,
        created_by="test",
        filename="changes.patch",
    )
    change_set = _put_json(
        artifacts,
        {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "base_revision": revision,
            "head_revision": revision,
            "has_effective_diff": True,
            "changed_files": [{"path": "app.py", "kind": "modified"}],
            "diff_artifact_id": str(diff.artifact_id),
        },
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.CHANGESET,
        filename="changeset.json",
    )
    permission = _put_json(
        artifacts,
        {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "base_revision": revision,
            "policy": {"allowed_paths": ["."], "denied_paths": [".git"]},
            "checked_paths": ["app.py"],
            "violations": [],
            "passed": True,
        },
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.PERMISSION_REPORT,
        filename="permission-report.json",
    )
    checks = [
        {
            "kind": kind,
            "name": name,
            "status": "passed",
            "detail": "passed",
            "evidence": [],
        }
        for kind, name in (
            ("diff", "effective_code_diff"),
            ("permission", "workspace_permissions"),
            ("command_policy", "forbidden_command_attempts"),
            ("static_analysis", "lint"),
            ("public_tests", "public"),
            ("hidden_tests", "hidden"),
        )
    ]
    verification = _put_json(
        artifacts,
        {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "passed": True,
            "checks": checks,
            "change_set_artifact_id": str(change_set.artifact_id),
            "diff_artifact_id": str(diff.artifact_id),
            "permission_artifact_id": str(permission.artifact_id),
            "command_audit_artifact_ids": [],
        },
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.VERIFICATION_REPORT,
        filename="verification-report.json",
    )
    review = _put_json(
        artifacts,
        {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "reviewer": "鲸鲸",
            "verdict": "approved",
            "issues": [],
            "summary": "证据充分，可以批准。",
        },
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.REVIEW_REPORT,
        filename="review-report.json",
    )
    condition = {
        "kind": "evidence_integrity",
        "passed": True,
        "detail": "all evidence is valid",
        "evidence": [
            verification.model_dump(mode="json"),
            review.model_dump(mode="json"),
        ],
    }
    completion = _put_json(
        artifacts,
        {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "passed": True,
            "failed_conditions": [],
            "conditions": [condition],
            "verification_artifact_id": str(verification.artifact_id),
            "review_artifact_id": str(review.artifact_id),
        },
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.COMPLETION_DECISION,
        filename="completion-decision.json",
    )
    for index, (event_type, payload) in enumerate(
        (
            (
                TraceEventType.VERIFICATION_COMPLETED,
                {"artifact_id": str(verification.artifact_id)},
            ),
            (
                TraceEventType.REVIEW_DECIDED,
                {
                    "artifact_ids": [
                        str(verification.artifact_id),
                        str(review.artifact_id),
                    ]
                },
            ),
            (
                TraceEventType.COMPLETION_DECIDED,
                {"artifact_id": str(completion.artifact_id)},
            ),
        )
    ):
        traces.append(
            TraceEvent(
                task_id=task_id,
                trace_id=trace_id,
                type=event_type,
                actor_kind=TraceActorKind.DETERMINISTIC,
                actor_id="test",
                idempotency_key=f"evidence-{index}",
                payload=payload,
            )
        )
    return artifacts, traces, task_id, trace_id, verification


def test_recovers_latest_evidence_and_restores_runtime(tmp_path: Path) -> None:
    artifacts, traces, task_id, trace_id, verification = _evidence_bundle(tmp_path)
    service = EvidenceRecoveryService(artifacts, traces)
    runtime = SimpleNamespace(
        task=SimpleNamespace(id=task_id, trace_id=trace_id),
        latest_verification=None,
        latest_completion=None,
    )

    recovered = service.restore_runtime(runtime)

    assert recovered.verification is runtime.latest_verification
    assert recovered.completion is runtime.latest_completion
    assert recovered.verification.artifact.artifact_id == verification.artifact_id
    assert recovered.verification.change_set.changed_files[0].path == "app.py"
    assert recovered.verification.permission_report.passed
    assert recovered.review.reviewer == "鲸鲸"
    assert recovered.completion.passed
    assert recovered.completion_sequence is not None


def test_does_not_restore_completion_older_than_latest_verification(tmp_path: Path) -> None:
    artifacts, traces, task_id, trace_id, verification = _evidence_bundle(tmp_path)
    traces.append(
        TraceEvent(
            task_id=task_id,
            trace_id=trace_id,
            type=TraceEventType.VERIFICATION_COMPLETED,
            actor_kind=TraceActorKind.DETERMINISTIC,
            actor_id="verifier",
            idempotency_key="new-verification",
            payload={"artifact_id": str(verification.artifact_id)},
        )
    )

    recovered = EvidenceRecoveryService(artifacts, traces).recover(
        task_id=task_id, trace_id=trace_id
    )

    assert recovered.verification is not None
    assert recovered.completion is None
    assert recovered.completion_sequence is None


def test_rejects_cross_task_artifact_recovery(tmp_path: Path) -> None:
    artifacts, traces, _, trace_id, verification = _evidence_bundle(tmp_path)

    with pytest.raises(EvidenceRecoveryError, match="crosses task or trace"):
        EvidenceRecoveryService(artifacts, traces).load_verification(
            verification.artifact_id,
            task_id=uuid4(),
            trace_id=trace_id,
        )


def test_rejects_corrupted_artifact_bytes(tmp_path: Path) -> None:
    artifacts, traces, task_id, trace_id, verification = _evidence_bundle(tmp_path)
    metadata = artifacts.get_metadata(verification.artifact_id)
    artifacts._blob_path(metadata.sha256).write_bytes(b"corrupted")

    with pytest.raises(EvidenceRecoveryError, match="invalid verification artifact"):
        EvidenceRecoveryService(artifacts, traces).recover(
            task_id=task_id, trace_id=trace_id
        )
