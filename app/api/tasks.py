from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.models import (
    ApiErrorResponse,
    CancelTaskRequest,
    CreateTaskRequest,
    TaskPage,
    TaskView,
)
from app.api.service import TaskService, TaskServiceUnavailable
from app.orchestration.models import TaskState

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

ERROR_RESPONSES = {
    404: {"model": ApiErrorResponse, "description": "Task not found"},
    409: {"model": ApiErrorResponse, "description": "Task state or revision conflict"},
    503: {"model": ApiErrorResponse, "description": "Task service unavailable"},
    422: {"model": ApiErrorResponse, "description": "Request validation failed"},
}


def get_task_service(request: Request) -> TaskService:
    service = getattr(request.app.state, "task_service", None)
    if service is None:
        raise TaskServiceUnavailable("task workflow service is not configured")
    return service


TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]


@router.post(
    "",
    response_model=TaskView,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: ERROR_RESPONSES[409],
        422: ERROR_RESPONSES[422],
        503: ERROR_RESPONSES[503],
    },
)
async def create_task(request: CreateTaskRequest, service: TaskServiceDependency) -> TaskView:
    return await service.create_task(request)


@router.get(
    "",
    response_model=TaskPage,
    responses={422: ERROR_RESPONSES[422], 503: ERROR_RESPONSES[503]},
)
async def list_tasks(
    service: TaskServiceDependency,
    state: TaskState | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskPage:
    return await service.list_tasks(state=state, limit=limit, offset=offset)


@router.get("/{task_id}", response_model=TaskView, responses=ERROR_RESPONSES)
async def get_task(task_id: UUID, service: TaskServiceDependency) -> TaskView:
    return await service.get_task(task_id)


@router.post("/{task_id}/cancel", response_model=TaskView, responses=ERROR_RESPONSES)
async def cancel_task(
    task_id: UUID, request: CancelTaskRequest, service: TaskServiceDependency
) -> TaskView:
    return await service.cancel_task(task_id, request)
