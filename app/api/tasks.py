from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status

from app.api.details import (
    ArtifactDetail,
    ContinueTaskPreflight,
    HumanMessageReceipt,
    PlanPage,
    RoomMessagePage,
    TaskControlView,
    TaskRoomView,
)
from app.api.events import EventStreamResponse, stream_task_events
from app.api.models import (
    ApiErrorResponse,
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
from app.api.service import TaskService, TaskServiceUnavailable
from app.orchestration.models import TaskState
from app.storage.continuation_authorizations import ContinuationAuthorizationReceipt
from app.storage.continuation_cancellations import ContinuationCancellationReceipt
from app.storage.continuation_workflows import ContinuationWorkflowOutcome
from app.storage.continuations import ContinuationQuarantineReceipt, ContinuationStatus

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

ERROR_RESPONSES = {
    404: {"model": ApiErrorResponse, "description": "Task not found"},
    409: {"model": ApiErrorResponse, "description": "Task state or revision conflict"},
    503: {"model": ApiErrorResponse, "description": "Task service unavailable"},
    422: {"model": ApiErrorResponse, "description": "Request validation failed"},
    500: {"model": ApiErrorResponse, "description": "Artifact integrity failure"},
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


@router.get("/{task_id}/room", response_model=TaskRoomView, responses=ERROR_RESPONSES)
async def get_task_room(task_id: UUID, service: TaskServiceDependency) -> TaskRoomView:
    return await service.get_room(task_id)


@router.get(
    "/{task_id}/messages", response_model=RoomMessagePage, responses=ERROR_RESPONSES
)
async def list_task_messages(
    task_id: UUID,
    service: TaskServiceDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RoomMessagePage:
    return await service.list_room_messages(
        task_id, after_sequence=after_sequence, limit=limit
    )


@router.post("/{task_id}/messages", response_model=HumanMessageReceipt,
             status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
async def post_human_message(
    task_id: UUID, request: PostHumanMessageRequest, service: TaskServiceDependency,
) -> HumanMessageReceipt:
    return await service.post_human_message(task_id, request)


@router.get("/{task_id}/plans", response_model=PlanPage, responses=ERROR_RESPONSES)
async def list_task_plans(task_id: UUID, service: TaskServiceDependency) -> PlanPage:
    return await service.list_plans(task_id)


@router.post("/{task_id}/continue/preflight", response_model=ContinueTaskPreflight,
             responses=ERROR_RESPONSES)
async def preflight_continue_task(
    task_id: UUID, request: ContinueTaskPreflightRequest, service: TaskServiceDependency,
) -> ContinueTaskPreflight:
    return await service.preflight_continue_task(task_id, request)


@router.get("/{task_id}/control", response_model=TaskControlView, responses=ERROR_RESPONSES)
async def get_task_control(task_id: UUID, service: TaskServiceDependency) -> TaskControlView:
    method = getattr(service, "get_task_control", None)
    if not callable(method):
        raise TaskServiceUnavailable("task control inspection is not configured")
    return await method(task_id)


@router.post("/{task_id}/continue", response_model=ContinuationStatus,
             status_code=status.HTTP_202_ACCEPTED, responses=ERROR_RESPONSES)
async def continue_task(
    task_id: UUID, request: ContinueTaskRequest, service: TaskServiceDependency,
) -> ContinuationStatus:
    method = getattr(service, "continue_task", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation execution is not configured")
    return await method(task_id, request)


@router.post("/{task_id}/continue/workflow", response_model=ContinuationStatus,
             status_code=status.HTTP_202_ACCEPTED, responses=ERROR_RESPONSES)
async def continue_task_workflow(
    task_id: UUID, request: ContinueTaskRequest, service: TaskServiceDependency,
) -> ContinuationStatus:
    method = getattr(service, "continue_task", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation workflow is not configured")
    return await method(task_id, request, workflow=True)


@router.get("/{task_id}/continuations/{request_id}/workflow",
            response_model=ContinuationWorkflowOutcome, responses=ERROR_RESPONSES)
async def get_continuation_workflow(
    task_id: UUID, request_id: UUID, service: TaskServiceDependency,
) -> ContinuationWorkflowOutcome:
    method = getattr(service, "get_continuation_workflow", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation workflow inspection is not configured")
    return await method(task_id, request_id)


@router.get("/{task_id}/continuations/{request_id}", response_model=ContinuationStatus,
            responses=ERROR_RESPONSES)
async def get_continuation(
    task_id: UUID, request_id: UUID, service: TaskServiceDependency,
) -> ContinuationStatus:
    method = getattr(service, "get_continuation", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation inspection is not configured")
    return await method(task_id, request_id)


@router.post("/{task_id}/continuations/{request_id}/quarantine",
             response_model=ContinuationQuarantineReceipt, responses=ERROR_RESPONSES)
async def quarantine_continuation(
    task_id: UUID, request_id: UUID, request: QuarantineContinuationRequest,
    service: TaskServiceDependency,
) -> ContinuationQuarantineReceipt:
    method = getattr(service, "quarantine_continuation", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation quarantine is not configured")
    return await method(task_id, request_id, request)


@router.post("/{task_id}/continuations/{request_id}/authorize",
             response_model=ContinuationAuthorizationReceipt, responses=ERROR_RESPONSES)
async def authorize_continuation(task_id: UUID, request_id: UUID,
                                 request: AuthorizeContinuationRequest, service: TaskServiceDependency):
    method = getattr(service, "authorize_continuation", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation authorization is not configured")
    return await method(task_id, request_id, request)


@router.get("/{task_id}/continuation-authorizations/{authorization_id}",
            response_model=ContinuationAuthorizationReceipt, responses=ERROR_RESPONSES)
async def get_continuation_authorization(task_id: UUID, authorization_id: UUID, service: TaskServiceDependency):
    method = getattr(service, "get_continuation_authorization", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation authorization is not configured")
    return await method(task_id, authorization_id)


@router.post("/{task_id}/continuations/{request_id}/cancel",
             response_model=ContinuationCancellationReceipt, status_code=status.HTTP_202_ACCEPTED,
             responses=ERROR_RESPONSES)
async def cancel_continuation(task_id: UUID, request_id: UUID,
                              request: CancelContinuationRequest, service: TaskServiceDependency):
    method = getattr(service, "cancel_continuation", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation cancellation is not configured")
    return await method(task_id, request_id, request)


@router.get("/{task_id}/continuations/{request_id}/cancellation",
            response_model=ContinuationCancellationReceipt, responses=ERROR_RESPONSES)
async def get_continuation_cancellation(task_id: UUID, request_id: UUID, service: TaskServiceDependency):
    method = getattr(service, "get_continuation_cancellation", None)
    if not callable(method):
        raise TaskServiceUnavailable("continuation cancellation is not configured")
    return await method(task_id, request_id)


@router.get(
    "/{task_id}/artifacts/{artifact_id}",
    response_model=ArtifactDetail,
    responses=ERROR_RESPONSES,
)
async def get_task_artifact(
    task_id: UUID, artifact_id: UUID, service: TaskServiceDependency
) -> ArtifactDetail:
    return await service.get_artifact(task_id, artifact_id)


@router.get(
    "/{task_id}/events",
    response_class=EventStreamResponse,
    responses={
        200: {"description": "Task trace event stream", "content": {"text/event-stream": {}}},
        404: ERROR_RESPONSES[404],
        422: ERROR_RESPONSES[422],
        503: ERROR_RESPONSES[503],
    },
)
async def task_events(
    task_id: UUID,
    request: Request,
    service: TaskServiceDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> EventStreamResponse:
    await service.get_task(task_id)  # Return a normal 404 before streaming starts.
    cursor = last_event_id if last_event_id is not None else after_sequence
    return EventStreamResponse(
        stream_task_events(request, service, task_id, after_sequence=cursor),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{task_id}/cancel", response_model=TaskView, responses=ERROR_RESPONSES)
async def cancel_task(
    task_id: UUID, request: CancelTaskRequest, service: TaskServiceDependency
) -> TaskView:
    return await service.cancel_task(task_id, request)
