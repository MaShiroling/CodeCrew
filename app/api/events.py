"""Cursor-resumable, task-scoped Server-Sent Events over persisted trace rows."""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.service import TaskService
from app.orchestration.models import TERMINAL_STATES
from app.trace import TraceEvent


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


def encode_trace_event(sequence: int, event: TraceEvent) -> str:
    payload = json.dumps(
        {"sequence": sequence, "event": event.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {sequence}\nevent: {event.type.value}\ndata: {payload}\n\n"


async def stream_task_events(
    request: Request,
    service: TaskService,
    task_id: UUID,
    *,
    after_sequence: int,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
    batch_size: int = 100,
) -> AsyncIterator[str]:
    """Replay stored rows, then follow new rows until terminal or disconnect."""
    if after_sequence < 0 or poll_seconds <= 0 or heartbeat_seconds <= 0 or batch_size <= 0:
        raise ValueError("SSE cursor, polling, heartbeat, and batch settings must be valid")
    cursor = after_sequence
    last_sent = monotonic()
    yield "retry: 3000\n\n"
    while not await request.is_disconnected():
        events = await service.list_trace_events(
            task_id, after_sequence=cursor, limit=batch_size
        )
        if events:
            for stored in events:
                if stored.sequence <= cursor:
                    raise RuntimeError("trace event source returned a non-increasing sequence")
                cursor = stored.sequence
                yield encode_trace_event(cursor, stored.event)
            last_sent = monotonic()
            continue
        task = await service.get_task(task_id)
        if task.state in TERMINAL_STATES:
            return
        if monotonic() - last_sent >= heartbeat_seconds:
            yield ": keepalive\n\n"
            last_sent = monotonic()
        await asyncio.sleep(poll_seconds)
