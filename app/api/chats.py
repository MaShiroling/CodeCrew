"""Standalone chat HTTP endpoints; deliberately separate from /tasks."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.chat_models import (
    ChatMessagePage,
    ChatMessageReceipt,
    ChatRoomPage,
    CreateChatRequest,
    PostChatMessageRequest,
)
from app.api.models import ApiErrorResponse
from app.chat.models import StandaloneChatRoom
from app.chat.service import ChatServiceUnavailable, StandaloneChatService

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


ChatServiceDependency = Annotated[StandaloneChatService, Depends(get_chat_service)]


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


@router.post("/{room_id}/messages", response_model=ChatMessageReceipt,
             status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
def post_chat_message(
    room_id: UUID, request: PostChatMessageRequest, service: ChatServiceDependency,
) -> ChatMessageReceipt:
    return ChatMessageReceipt(message=service.post_message(
        room_id, content=request.content, idempotency_key=request.idempotency_key,
        reply_to=request.reply_to,
    ))
