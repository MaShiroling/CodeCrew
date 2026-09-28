from typing import Protocol
from uuid import UUID

from app.api.details import (
    ArtifactDetail,
    ContinueTaskPreflight,
    HumanMessageReceipt,
    PlanPage,
    RoomMessagePage,
    TaskControlView,
    TaskRoomView,
)
from app.api.models import (
    AuthorizeContinuationRequest,
    CancelContinuationRequest,
    CancelTaskRequest,
    ContinueTaskPreflightRequest,
    ContinueTaskRequest,
    CreateTaskRequest,
    PostHumanMessageRequest,
    QuarantineContinuationRequest,
    TaskPage,
    TaskView,
)
from app.orchestration.models import TaskState
from app.storage.continuation_authorizations import ContinuationAuthorizationReceipt
from app.storage.continuation_cancellations import ContinuationCancellationReceipt
from app.storage.continuations import ContinuationQuarantineReceipt, ContinuationStatus
from app.trace import StoredTraceEvent


class TaskApiServiceError(RuntimeError):
    code = "task_service_error"
    status_code = 500


class TaskNotFound(TaskApiServiceError):
    code = "task_not_found"
    status_code = 404


class TaskStateConflict(TaskApiServiceError):
    code = "task_state_conflict"
    status_code = 409


class TaskServiceUnavailable(TaskApiServiceError):
    code = "task_service_unavailable"
    status_code = 503


class TaskInvalidRepository(TaskApiServiceError):
    code = "invalid_repository"
    status_code = 422


class TaskDetailUnavailable(TaskApiServiceError):
    code = "task_detail_unavailable"
    status_code = 409


class TaskArtifactNotFound(TaskApiServiceError):
    code = "task_artifact_not_found"
    status_code = 404


class TaskArtifactIntegrityError(TaskApiServiceError):
    code = "task_artifact_integrity_error"
    status_code = 500


class TaskMessageNotFound(TaskApiServiceError):
    code = "task_message_not_found"
    status_code = 404


class TaskMessageInvalid(TaskApiServiceError):
    code = "invalid_human_message"
    status_code = 422


class TaskMessageConflict(TaskApiServiceError):
    code = "task_message_conflict"
    status_code = 409


class TaskContinuationNotFound(TaskApiServiceError):
    code = "task_continuation_not_found"
    status_code = 404


class TaskService(Protocol):
    """Boundary between HTTP transport and durable task workflow operations."""

    async def create_task(self, request: CreateTaskRequest) -> TaskView: ...

    async def get_task(self, task_id: UUID) -> TaskView: ...

    async def list_tasks(
        self, *, state: TaskState | None, limit: int, offset: int
    ) -> TaskPage: ...

    async def cancel_task(
        self, task_id: UUID, request: CancelTaskRequest
    ) -> TaskView: ...

    async def list_trace_events(
        self, task_id: UUID, *, after_sequence: int, limit: int
    ) -> tuple[StoredTraceEvent, ...]: ...

    async def get_room(self, task_id: UUID) -> TaskRoomView: ...

    async def list_room_messages(
        self, task_id: UUID, *, after_sequence: int, limit: int
    ) -> RoomMessagePage: ...

    async def post_human_message(
        self, task_id: UUID, request: PostHumanMessageRequest,
    ) -> HumanMessageReceipt: ...

    async def preflight_continue_task(
        self, task_id: UUID, request: ContinueTaskPreflightRequest,
    ) -> ContinueTaskPreflight: ...

    async def get_task_control(self, task_id: UUID) -> TaskControlView: ...

    async def get_continuation(self, task_id: UUID, request_id: UUID) -> ContinuationStatus: ...

    async def continue_task(self, task_id: UUID, request: ContinueTaskRequest) -> ContinuationStatus: ...

    async def authorize_continuation(self, task_id: UUID, request_id: UUID,
                                     request: AuthorizeContinuationRequest) -> ContinuationAuthorizationReceipt: ...

    async def get_continuation_authorization(self, task_id: UUID, authorization_id: UUID) -> ContinuationAuthorizationReceipt: ...

    async def cancel_continuation(self, task_id: UUID, request_id: UUID,
                                  request: CancelContinuationRequest) -> ContinuationCancellationReceipt: ...

    async def get_continuation_cancellation(self, task_id: UUID, request_id: UUID) -> ContinuationCancellationReceipt: ...

    async def quarantine_continuation(
        self, task_id: UUID, request_id: UUID, request: QuarantineContinuationRequest,
    ) -> ContinuationQuarantineReceipt: ...

    async def list_plans(self, task_id: UUID) -> PlanPage: ...

    async def get_artifact(self, task_id: UUID, artifact_id: UUID) -> ArtifactDetail: ...
