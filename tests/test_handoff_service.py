from pathlib import Path
from uuid import uuid4

import pytest

from app.messaging import (
    ArtifactReferenceValidationError,
    HandoffEnvelope,
    HandoffParty,
    HandoffService,
    HandoffType,
    Mailbox,
    MailboxMessageStatus,
)
from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase


def make_service(tmp_path: Path) -> HandoffService:
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    service = HandoffService(
        Mailbox(database),
        ArtifactStore(database, tmp_path / "artifacts"),
    )
    service.initialize()
    return service


def make_envelope(
    service: HandoffService,
    *,
    content: str = "implementation plan",
) -> tuple[HandoffEnvelope, ArtifactReference]:
    task_id = uuid4()
    trace_id = uuid4()
    artifact = service.artifacts.put_text(
        content,
        task_id=task_id,
        trace_id=trace_id,
        type=ArtifactType.PLAN,
        created_by="planner",
    )
    reference = ArtifactReference.from_metadata(artifact, summary="Implementation plan")
    envelope = HandoffEnvelope(
        task_id=task_id,
        trace_id=trace_id,
        idempotency_key=f"plan:{task_id}",
        sender=HandoffParty.PLANNER,
        recipient=HandoffParty.IMPLEMENTER,
        type=HandoffType.PLAN_READY,
        artifacts=(reference,),
    )
    return envelope, reference


def test_validated_handoff_round_trip(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    envelope, _ = make_envelope(service)

    sent = service.send(envelope)
    batch = service.receive(HandoffParty.IMPLEMENTER)

    assert sent.status is MailboxMessageStatus.PENDING
    assert len(batch.accepted) == 1
    assert batch.accepted[0].envelope == envelope
    assert batch.rejected == ()


def test_handoff_without_artifacts_is_valid(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task_id = uuid4()
    envelope = HandoffEnvelope(
        task_id=task_id,
        trace_id=uuid4(),
        idempotency_key=f"failure:{task_id}",
        sender=HandoffParty.ORCHESTRATOR,
        recipient=HandoffParty.HUMAN,
        type=HandoffType.TASK_FAILED,
    )

    service.send(envelope)

    assert service.receive(HandoffParty.HUMAN).accepted[0].envelope == envelope


@pytest.mark.parametrize(
    ("changed_field", "expected"),
    [
        ("task_id", "task_id"),
        ("trace_id", "trace_id"),
    ],
)
def test_send_rejects_artifact_from_other_context(
    tmp_path: Path, changed_field: str, expected: str
) -> None:
    service = make_service(tmp_path)
    envelope, _ = make_envelope(service)
    invalid = envelope.model_copy(update={changed_field: uuid4()})

    with pytest.raises(ArtifactReferenceValidationError, match=expected):
        service.send(invalid)

    assert service.mailbox.list_messages() == ()


def test_send_rejects_missing_mismatched_and_duplicate_references(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    envelope, reference = make_envelope(service)
    missing = reference.model_copy(update={"artifact_id": uuid4()})
    wrong_hash = reference.model_copy(update={"sha256": "f" * 64})
    wrong_type = reference.model_copy(update={"type": ArtifactType.DIFF})

    with pytest.raises(ArtifactReferenceValidationError, match="does not exist"):
        service.send(envelope.model_copy(update={"artifacts": (missing,)}))
    with pytest.raises(ArtifactReferenceValidationError, match="sha256"):
        service.send(envelope.model_copy(update={"artifacts": (wrong_hash,)}))
    with pytest.raises(ArtifactReferenceValidationError, match="type"):
        service.send(envelope.model_copy(update={"artifacts": (wrong_type,)}))
    with pytest.raises(ArtifactReferenceValidationError, match="duplicate"):
        service.send(envelope.model_copy(update={"artifacts": (reference, reference)}))


def test_send_detects_corrupt_blob(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    envelope, reference = make_envelope(service)
    service.artifacts.blob_path_for(reference.artifact_id).write_text("tampered")

    with pytest.raises(ArtifactReferenceValidationError, match="integrity check failed"):
        service.send(envelope)


def test_receive_marks_message_failed_if_blob_changes_after_send(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    envelope, reference = make_envelope(service)
    service.send(envelope)
    service.artifacts.blob_path_for(reference.artifact_id).unlink()

    batch = service.receive(HandoffParty.IMPLEMENTER)

    assert batch.accepted == ()
    assert len(batch.rejected) == 1
    assert batch.rejected[0].status is MailboxMessageStatus.FAILED
    assert "artifact validation failed" in (batch.rejected[0].failure_reason or "")


def test_service_requires_one_shared_database(tmp_path: Path) -> None:
    mailbox = Mailbox(SQLiteDatabase(tmp_path / "mailbox.sqlite3"))
    artifacts = ArtifactStore(
        SQLiteDatabase(tmp_path / "artifacts.sqlite3"), tmp_path / "artifacts"
    )

    with pytest.raises(ValueError, match="same database"):
        HandoffService(mailbox, artifacts)
