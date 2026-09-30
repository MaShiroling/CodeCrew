"""Read-only SSE invalidations for independent chat rooms.

Messages and turn states remain authoritative in SQLite. SSE only tells a UI
to reload those resources; reconnection starts with an immediate snapshot.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request

from app.chat.service import StandaloneChatService


def _changed_frame(message_sequence: int, turn_count: int) -> str:
    data = json.dumps(
        {"message_sequence": message_sequence, "turn_count": turn_count},
        separators=(",", ":"),
    )
    return f"event: chat_changed\ndata: {data}\n\n"


async def stream_chat_activity(
    request: Request,
    service: StandaloneChatService,
    room_id: UUID,
    *,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Follow persisted message/turn changes; never infer Agent success."""
    if poll_seconds <= 0 or heartbeat_seconds <= 0:
        raise ValueError("SSE polling and heartbeat intervals must be positive")
    cursor = 0
    turn_fingerprint: tuple[tuple[str, str, str], ...] | None = None
    last_sent = monotonic()
    yield "retry: 3000\n\n"
    while not await request.is_disconnected():
        messages = service.list_messages(room_id, after_sequence=cursor, limit=100)
        if messages:
            cursor = messages[-1].sequence
        turns = service.store.list_turns(room_id)
        current = tuple(
            (str(turn.turn_id), turn.status.value, turn.updated_at.isoformat())
            for turn in turns
        )
        if messages or current != turn_fingerprint:
            turn_fingerprint = current
            yield _changed_frame(cursor, len(turns))
            last_sent = monotonic()
            if len(messages) == 100:
                continue
        elif monotonic() - last_sent >= heartbeat_seconds:
            yield ": keepalive\n\n"
            last_sent = monotonic()
        await asyncio.sleep(poll_seconds)
