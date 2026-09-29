from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.orchestration.models import utc_now


class AgentRole(str, Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


class AgentCapability(str, Enum):
    REPOSITORY_ANALYSIS = "repository_analysis"
    CODE_EDIT = "code_edit"
    COMMAND_EXECUTION = "command_execution"
    CODE_REVIEW = "code_review"
    STREAMING = "streaming"
    SESSION_RESUME = "session_resume"


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


class AgentSessionStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentEventType(str, Enum):
    STARTED = "started"
    STDOUT = "stdout"
    STDERR = "stderr"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentExitReason(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"


class AgentArtifactInput(BaseModel):
    """Trusted orchestration grant for one integrity-bound, read-only input file.

    Not an Agent output field or a public permission-grant API.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    task_id: UUID
    trace_id: UUID
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_absolute_path(cls, path: Path) -> Path:
        if not path.is_absolute():
            raise ValueError("artifact input path must be absolute")
        return path


class AgentRequest(BaseModel):
    """Provider-neutral input for starting or resuming an agent session."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    trace_id: UUID
    role: AgentRole
    prompt: str = Field(min_length=1)
    working_directory: Path
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    timeout_seconds: int = Field(default=900, gt=0)
    resume_from_session_id: str | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_inputs: tuple[AgentArtifactInput, ...] = Field(default=(), max_length=1000)
    # Internal execution contract, not a user-authored prompt or success verdict.
    output_schema: dict[str, Any] | None = None
    # Set only by a trusted controller/caller, never inferred from Agent prose.
    clarification_only: bool = False
    discussion_only: bool = False
    # Internal chat execution context. task_id remains an adapter execution namespace,
    # not a persisted Task ID, when this field is set.
    standalone_chat_room_id: UUID | None = None
    runtime_directory: Path | None = None

    @model_validator(mode="after")
    def validate_artifact_inputs(self) -> "AgentRequest":
        if self.clarification_only and (
            self.role is not AgentRole.IMPLEMENTER
            or self.permission_mode is not PermissionMode.READ_ONLY
        ):
            raise ValueError("clarification-only requests require a read-only implementer")
        if self.discussion_only and (
            self.clarification_only or self.permission_mode is not PermissionMode.READ_ONLY
        ):
            raise ValueError("discussion-only requests require read-only permission")
        if self.standalone_chat_room_id is not None:
            if (
                not self.discussion_only
                or self.permission_mode is not PermissionMode.READ_ONLY
                or self.artifact_inputs
                or self.resume_from_session_id is not None
                or self.runtime_directory is None
                or not self.runtime_directory.is_absolute()
                or self.runtime_directory == self.working_directory
            ):
                raise ValueError(
                    "standalone chat requires a fresh read-only discussion and private runtime"
                )
        elif self.runtime_directory is not None:
            raise ValueError("runtime_directory is reserved for standalone chat")
        if any(
            item.task_id != self.task_id or item.trace_id != self.trace_id
            for item in self.artifact_inputs
        ):
            raise ValueError("artifact inputs must belong to the request task and trace")
        ids = [item.artifact_id for item in self.artifact_inputs]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact inputs must have unique IDs")
        return self


class AgentSession(BaseModel):
    """A CodeCrew session mapped to an optional provider-native session."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    agent_name: str = Field(min_length=1)
    role: AgentRole
    status: AgentSessionStatus = AgentSessionStatus.STARTING
    native_session_id: str | None = Field(default=None, min_length=1)
    started_at: AwareDatetime = Field(default_factory=utc_now)


class AgentEvent(BaseModel):
    """One normalized event emitted by an adapter."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    trace_id: UUID
    sequence: int = Field(ge=0)
    type: AgentEventType
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    native_event_type: str | None = None


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class AgentResult(BaseModel):
    """Execution facts only; this does not declare the software task successful."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    trace_id: UUID
    reason: AgentExitReason
    exit_code: int | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage | None = None
    duration_ms: int = Field(ge=0)
    error: str | None = None
