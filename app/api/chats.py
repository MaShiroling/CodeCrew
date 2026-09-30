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
        reply_to=request.reply_to,
    )
    turns = dispatcher.enqueue(message) if dispatcher is not None else ()
    return ChatMessageReceipt(
        message=message, discussion_queued=any(turn.status.value == "queued" for turn in turns),
        turns=turns,
    )
