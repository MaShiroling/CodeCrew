from typing import Protocol
from uuid import UUID

from app.api.models import CancelTaskRequest, CreateTaskRequest, TaskPage, TaskView
from app.orchestration.models import TaskState


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
