"""Only locally derived display excerpts are bounded, not stored evidence."""

import hashlib

import pytest
from pydantic import ValidationError

from app.storage import ArtifactReference, ArtifactType
from app.storage.models import (
    ARTIFACT_SUMMARY_MAX_LENGTH,
    ARTIFACT_SUMMARY_TRUNCATION_MARKER,
)
from app.verification import ReviewReport
from tests.test_artifact_store import common_fields, make_store


@pytest.mark.parametrize("length", [999, 1000, 1001, 1158, 16000])
@pytest.mark.parametrize("character", ["x", "评", "🐳"])
def test_derived_summary_is_bounded_without_mutating_artifact(tmp_path, length, character):
    store = make_store(tmp_path)
    summary = character * length
    artifact = store.put_text(summary, type=ArtifactType.GENERIC, **common_fields())
    reference = store.get_reference(artifact.artifact_id, summary=summary)
    assert reference.artifact_id == artifact.artifact_id
    assert reference.type is artifact.type
    assert reference.sha256 == artifact.sha256
    assert len(reference.summary) <= ARTIFACT_SUMMARY_MAX_LENGTH
    if length <= ARTIFACT_SUMMARY_MAX_LENGTH:
        assert reference.summary == summary
    else:
        assert reference.summary.endswith(ARTIFACT_SUMMARY_TRUNCATION_MARKER)
        prefix = reference.summary.removesuffix(ARTIFACT_SUMMARY_TRUNCATION_MARKER)
        assert summary.startswith(prefix)
    assert store.read_text(artifact.artifact_id) == summary
    assert store.get_metadata(artifact.artifact_id) == artifact
    assert hashlib.sha256(store.blob_path_for(artifact.artifact_id).read_bytes()).hexdigest() == (
        artifact.sha256
    )


def test_strip_before_bounding_does_not_truncate_valid_short_summary(tmp_path):
    artifact = make_store(tmp_path).put_text("report", type=ArtifactType.GENERIC, **common_fields())
    summary = "评" * 1000
    assert ArtifactReference.from_metadata(artifact, summary=" \n" + summary + " \n").summary == summary


@pytest.mark.parametrize("summary", ["", " \n", None, 123])
def test_invalid_summary_is_not_repaired_or_replaced(tmp_path, summary):
    artifact = make_store(tmp_path).put_text("report", type=ArtifactType.GENERIC, **common_fields())
    with pytest.raises(ValidationError):
        ArtifactReference.from_metadata(artifact, summary=summary)


def test_external_reference_validation_keeps_schema_limit(tmp_path):
    artifact = make_store(tmp_path).put_text("report", type=ArtifactType.GENERIC, **common_fields())
    ref = ArtifactReference.from_metadata(artifact, summary="report")
    payload = ref.model_dump(mode="json")
    payload["summary"] = "x" * 1001
    with pytest.raises(ValidationError, match="at most 1000"):
        ArtifactReference.model_validate(payload)


def test_reference_excerpt_does_not_relax_full_review_report_schema(tmp_path):
    summary = "评" * 4001
    artifact = make_store(tmp_path).put_text(
        summary, type=ArtifactType.REVIEW_REPORT, **common_fields()
    )
    reference = ArtifactReference.from_metadata(artifact, summary=summary)
    with pytest.raises(ValidationError, match="at most 4000"):
        ReviewReport(
            task_id=artifact.task_id, trace_id=artifact.trace_id,
            reviewer="reviewer", verdict="approved", summary=summary, artifact=reference,
        )
