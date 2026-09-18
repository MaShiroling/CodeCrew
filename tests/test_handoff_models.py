from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.messaging import (
    HANDOFF_PROTOCOL_VERSION,
    MAX_HANDOFF_ARTIFACTS,
    MAX_HANDOFF_PAYLOAD_BYTES,
    HandoffEnvelope,
    HandoffParty,
    HandoffType,
    MailboxMessageStatus,
)
from app.storage import ArtifactMetadata, ArtifactReference, ArtifactType


def make_reference() -> ArtifactReference:
    metadata = ArtifactMetadata(
        task_id=uuid4(),
        trace_id=uuid4(),
        type=ArtifactType.PLAN,
        media_type="application/json",
        sha256="b" * 64,
        size_bytes=42,
        created_by="planner",
    )
    return ArtifactReference.from_metadata(metadata, summary="Implementation plan")


def make_envelope(**updates: object) -> HandoffEnvelope:
    values: dict[str, object] = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "idempotency_key": "task-1:plan:attempt-1",
        "sender": HandoffParty.PLANNER,
        "recipient": HandoffParty.IMPLEMENTER,
        "type": HandoffType.PLAN_READY,
        "payload": {"summary": "Implement parser fix", "attempt": 1},
        "artifacts": (make_reference(),),
    }
    values.update(updates)
    return HandoffEnvelope(**values)


def test_handoff_round_trip_preserves_protocol_and_references() -> None:
    envelope = make_envelope()

    restored = HandoffEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert restored.protocol_version == HANDOFF_PROTOCOL_VERSION
    assert restored.artifacts[0].type is ArtifactType.PLAN
    assert restored.created_at.tzinfo is not None


def test_handoff_rejects_unknown_protocol_version() -> None:
    with pytest.raises(ValidationError):
        make_envelope(protocol_version="2.0")


def test_handoff_payload_must_be_json_serializable() -> None:
    with pytest.raises(ValidationError):
        make_envelope(payload={"invalid": {1, 2, 3}})


def test_large_content_must_use_artifact_reference() -> None:
    oversized = "x" * (MAX_HANDOFF_PAYLOAD_BYTES + 1)

    with pytest.raises(ValidationError, match="use an artifact reference"):
        make_envelope(payload={"diff": oversized})


def test_handoff_rejects_too_many_artifacts() -> None:
    reference = make_reference()

    with pytest.raises(ValidationError):
        make_envelope(artifacts=(reference,) * (MAX_HANDOFF_ARTIFACTS + 1))


def test_handoff_route_requires_distinct_parties() -> None:
    with pytest.raises(ValidationError, match="must be different"):
        make_envelope(recipient=HandoffParty.PLANNER)


def test_handoff_cannot_cause_itself() -> None:
    message_id = uuid4()

    with pytest.raises(ValidationError, match="cannot cause itself"):
        make_envelope(message_id=message_id, causation_id=message_id)


def test_mailbox_status_vocabulary_is_explicit() -> None:
    assert {status.value for status in MailboxMessageStatus} == {
        "pending",
        "delivered",
        "acknowledged",
        "failed",
    }

