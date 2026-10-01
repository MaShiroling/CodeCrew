"""Standalone chat HTTP endpoints; deliberately separate from /tasks."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.chat_events import stream_chat_activity
from app.api.chat_models import (
    ChatMessagePage,
    ChatMessageReceipt,
    ChatRoomPage,
    ChatTurnPage,
    CreateChatRequest,
    PostChatMessageRequest,
)
from app.api.events import EventStreamResponse
from app.api.models import ApiErrorResponse
from app.chat.coding_authorization import ChatCodingAuthorizationService, ChatCodingUnavailable
from app.chat.coding_intent import (
    AuthorizeChatCodingTaskRequest,
    AuthorizedCodingTask,
    ChatCodingCapability,
    CodingTaskDraft,
    CodingTaskPreflight,
    preflight_coding_task,
)
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.models import StandaloneChatRoom, StandaloneChatTurn
from app.chat.service import ChatMessageNotFound, ChatServiceUnavailable, StandaloneChatService
from app.chat.store import StandaloneChatMessageNotFoundError

router = APIRouter(prefix="/api/v1/chats", tags=["standalone chats"])
ERROR_RESPONSES = {
    404: {"model": ApiErrorResponse},
    409: {"model": ApiErrorResponse},
    422: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


def get_chat_service(request: Request) -> StandaloneChatService:
    service = getattr(request.app.state, "chat_service", None)
    if service is None:
        raise ChatServiceUnavailable("standalone chat service is not configured")
    return service


def get_chat_dispatcher(request: Request) -> StandaloneChatDispatcher | None:
    return getattr(request.app.state, "chat_dispatcher", None)


ChatServiceDependency = Annotated[StandaloneChatService, Depends(get_chat_service)]
ChatDispatcherDependency = Annotated[StandaloneChatDispatcher | None, Depends(get_chat_dispatcher)]


def get_chat_coding_service(request: Request) -> ChatCodingAuthorizationService:
    service = getattr(request.app.state, "chat_coding_service", None)
    if service is None:
        raise ChatCodingUnavailable("chat-to-code authorization is not configured")
    return service


def get_optional_chat_coding_service(
    request: Request,
) -> ChatCodingAuthorizationService | None:
    return getattr(request.app.state, "chat_coding_service", None)


ChatCodingDependency = Annotated[
    ChatCodingAuthorizationService, Depends(get_chat_coding_service),
]


@router.get("/coding-capability", response_model=ChatCodingCapability,
            response_model_exclude_none=True)
def coding_capability(request: Request) -> ChatCodingCapability:
    service = getattr(request.app.state, "chat_coding_service", None)
    return ChatCodingCapability(
        available=service is not None,
        allowed_paths=service.policy.allowed_paths if service is not None else (),
        demo_repository_path=(str(service.repository_bound)
                              if service is not None and service.repository_bound else None),
        demo_issue=service.issue_bound if service is not None else None,
    )


@router.post("", response_model=StandaloneChatRoom, status_code=status.HTTP_201_CREATED,
             responses=ERROR_RESPONSES)
def create_chat(request: CreateChatRequest, service: ChatServiceDependency) -> StandaloneChatRoom:
    return service.create_room(title=request.title, idempotency_key=request.idempotency_key)


@router.get("", response_model=ChatRoomPage, responses=ERROR_RESPONSES)
def list_chats(
    service: ChatServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatRoomPage:
    return ChatRoomPage(items=service.list_rooms(limit=limit, offset=offset),
                        limit=limit, offset=offset)


@router.get("/{room_id}", response_model=StandaloneChatRoom, responses=ERROR_RESPONSES)
def get_chat(room_id: UUID, service: ChatServiceDependency) -> StandaloneChatRoom:
    return service.get_room(room_id)


@router.post("/{room_id}/coding-task-preflight", response_model=CodingTaskPreflight,
             responses=ERROR_RESPONSES)
def preflight_chat_coding_task(
    room_id: UUID, request: CodingTaskDraft, service: ChatServiceDependency,
    authorization: Annotated[ChatCodingAuthorizationService | None,
                             Depends(get_optional_chat_coding_service)],
) -> CodingTaskPreflight:
    """Read-only Human review snapshot; never creates or authorizes a coding Task."""
    if authorization is not None:
        return authorization.preflight(room_id, request)
    return preflight_coding_task(service, room_id, request)


@router.post("/{room_id}/coding-tasks", response_model=AuthorizedCodingTask,
             status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
async def authorize_chat_coding_task(
    room_id: UUID, request: AuthorizeChatCodingTaskRequest,
    service: ChatCodingDependency,
) -> AuthorizedCodingTask:
    """Explicit Human command; chat messages and preflight never invoke it."""
    return await service.authorize(room_id, request)


@router.get("/{room_id}/messages", response_model=ChatMessagePage,
            responses=ERROR_RESPONSES)
def list_chat_messages(
    room_id: UUID,
    service: ChatServiceDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ChatMessagePage:
    return ChatMessagePage(
        items=service.list_messages(room_id, after_sequence=after_sequence, limit=limit),
        after_sequence=after_sequence, limit=limit,
    )


@router.get("/{room_id}/turns", response_model=ChatTurnPage, responses=ERROR_RESPONSES)
def list_chat_turns(room_id: UUID, service: ChatServiceDependency) -> ChatTurnPage:
    service.get_room(room_id)
    return ChatTurnPage(items=service.store.list_turns(room_id))


@router.get("/{room_id}/events", response_class=EventStreamResponse,
            responses=ERROR_RESPONSES)
def chat_events(
    room_id: UUID, request: Request, service: ChatServiceDependency,
) -> EventStreamResponse:
    service.get_room(room_id)  # Return a real 404 before opening the stream.
    return EventStreamResponse(
        stream_chat_activity(request, service, room_id),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{room_id}/turns/{turn_id}/cancel", response_model=StandaloneChatTurn,
             responses=ERROR_RESPONSES)
async def cancel_chat_turn(
    room_id: UUID, turn_id: UUID, service: ChatServiceDependency,
    dispatcher: ChatDispatcherDependency,
) -> StandaloneChatTurn:
    service.get_room(room_id)
    if dispatcher is None:
        raise ChatServiceUnavailable("standalone chat dispatcher is not configured")
    try:
        turn = service.store.get_turn(turn_id)
    except StandaloneChatMessageNotFoundError as exc:
        raise ChatMessageNotFound("chat turn not found") from exc
    if turn.room_id != room_id:
        raise ChatMessageNotFound("chat turn not found")
    await dispatcher.cancel(turn_id)
    return service.store.get_turn(turn_id)


@router.post("/{room_id}/messages", response_model=ChatMessageReceipt,
             status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
async def post_chat_message(
    room_id: UUID, request: PostChatMessageRequest, service: ChatServiceDependency,
    dispatcher: ChatDispatcherDependency,
) -> ChatMessageReceipt:
    message = service.post_message(
        room_id, content=request.content, idempotency_key=request.idempotency_key,
        reply_to=request.reply_to, context_anchor_id=request.context_anchor_id,
    )
    turns = dispatcher.enqueue(message) if dispatcher is not None else ()
    return ChatMessageReceipt(
        message=message, discussion_queued=any(turn.status.value == "queued" for turn in turns),
        turns=turns,
    )
