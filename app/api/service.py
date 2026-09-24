from typing import Protocol
from uuid import UUID

from app.api.details import ArtifactDetail, PlanPage, RoomMessagePage, TaskRoomView
from app.api.models import CancelTaskRequest, CreateTaskRequest, TaskPage, TaskView
from app.orchestration.models import TaskState
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

    async def list_plans(self, task_id: UUID) -> PlanPage: ...

    async def get_artifact(self, task_id: UUID, artifact_id: UUID) -> ArtifactDetail: ...
