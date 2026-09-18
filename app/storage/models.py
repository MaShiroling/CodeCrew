from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.orchestration.models import utc_now


class ArtifactType(str, Enum):
    PLAN = "plan"
    DIFF = "diff"
    CHANGESET = "changeset"
    PERMISSION_REPORT = "permission_report"
    TEST_LOG = "test_log"
    VERIFICATION_REPORT = "verification_report"
    REVIEW_REPORT = "review_report"
    TASK_REPORT = "task_report"
    GENERIC = "generic"


class ArtifactMetadata(BaseModel):
    """Immutable metadata for content stored outside A2A messages."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    artifact_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    type: ArtifactType
    media_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    created_by: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("filename must be a basename without path separators")
        return value


class ArtifactReference(BaseModel):
    """Small integrity-bound reference suitable for a handoff envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    artifact_id: UUID
    type: ArtifactType
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=1000)

    @classmethod
    def from_metadata(cls, artifact: ArtifactMetadata, *, summary: str) -> "ArtifactReference":
        return cls(
            artifact_id=artifact.artifact_id,
            type=artifact.type,
            sha256=artifact.sha256,
            summary=summary,
        )
