from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.storage import ArtifactMetadata, ArtifactReference, ArtifactType

VALID_SHA256 = "a" * 64


def make_metadata(**updates: object) -> ArtifactMetadata:
    values: dict[str, object] = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "type": ArtifactType.PLAN,
        "media_type": "application/json",
        "sha256": VALID_SHA256,
        "size_bytes": 128,
        "created_by": "planner",
        "filename": "plan.json",
    }
    values.update(updates)
    return ArtifactMetadata(**values)


def test_artifact_reference_is_derived_from_integrity_metadata() -> None:
    metadata = make_metadata()

    reference = ArtifactReference.from_metadata(
        metadata,
        summary="Structured implementation plan",
    )

    assert reference.artifact_id == metadata.artifact_id
    assert reference.type is ArtifactType.PLAN
    assert reference.sha256 == metadata.sha256


@pytest.mark.parametrize("sha256", ["abc", "A" * 64, "g" * 64])
def test_artifact_hash_must_be_lowercase_sha256(sha256: str) -> None:
    with pytest.raises(ValidationError):
        make_metadata(sha256=sha256)


@pytest.mark.parametrize("filename", ["../plan.json", "logs/plan.json", r"logs\plan.json", ".."])
def test_artifact_filename_rejects_paths(filename: str) -> None:
    with pytest.raises(ValidationError, match="basename"):
        make_metadata(filename=filename)


def test_artifact_metadata_is_immutable() -> None:
    metadata = make_metadata()

    with pytest.raises(ValidationError):
        metadata.size_bytes = 999

