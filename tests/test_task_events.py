import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.events import stream_task_events
from app.api.models import TaskView
from app.api.service import TaskNotFound
from app.main import create_app
from app.orchestration.models import Task, TaskState
from app.trace import StoredTraceEvent, TraceActorKind, TraceEvent, TraceEventType


class EventService:
    def __init__(self, *, state: TaskState = TaskState.COMPLETED) -> None:
        task = Task(issue="Fix parser", repository_path="/tmp/repository", state=state)
        self.task = TaskView(
            task_id=task.id,
            trace_id=task.trace_id,
            issue=task.issue,
            repository_path=task.repository_path,
            state=task.state,
            rework_rounds=0,
            revision=1,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        self.events: list[StoredTraceEvent] = []
        self.queries: list[int] = []

    async def get_task(self, task_id):
        if task_id != self.task.task_id:
            raise TaskNotFound("task not found")
        return self.task

    async def list_trace_events(self, task_id, *, after_sequence, limit):
        assert task_id == self.task.task_id
        self.queries.append(after_sequence)
        return tuple(event for event in self.events if event.sequence > after_sequence)[:limit]

    def append(self, sequence: int, *, content: str = "") -> None:
        self.events.append(
            StoredTraceEvent(
                sequence=sequence,
                event=TraceEvent(
                    task_id=self.task.task_id,
                    trace_id=self.task.trace_id,
                    type=TraceEventType.CHAT_MESSAGE_PERSISTED,
                    actor_kind=TraceActorKind.AGENT,
                    actor_id="planner",
                    idempotency_key=f"event-{sequence}",
                    payload={"content": content},
                ),
            )
        )


def test_sse_replays_events_and_resumes_from_last_event_id() -> None:
    service = EventService()
    service.append(1, content="first\nsecond")
    service.append(3, content="later")
    client = TestClient(create_app(task_service=service))
    path = f"/api/v1/tasks/{service.task.task_id}/events"

    full = client.get(path)
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\nevent: chat_message_persisted\ndata: " in full.text
    assert "id: 3\nevent: chat_message_persisted\ndata: " in full.text
    assert "first\\nsecond" in full.text
    assert "id: 2" not in full.text

    resumed = client.get(path, params={"after_sequence": 0}, headers={"Last-Event-ID": "1"})
    assert resumed.status_code == 200
    assert "id: 1" not in resumed.text
    assert "id: 3" in resumed.text
    assert service.queries[-2:] == [1, 3]


def test_sse_rejects_invalid_cursor_and_missing_task_before_streaming() -> None:
    service = EventService()
    client = TestClient(create_app(task_service=service))
    path = f"/api/v1/tasks/{service.task.task_id}/events"

    invalid = client.get(path, headers={"Last-Event-ID": "-1"})
    missing = client.get(f"/api/v1/tasks/{uuid4()}/events")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"


class ConnectedRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.mark.asyncio
async def test_sse_follows_new_event_then_closes_when_terminal() -> None:
    service = EventService(state=TaskState.PLANNING)
    request = ConnectedRequest()
    stream = stream_task_events(
        request, service, service.task.task_id,
        after_sequence=0, poll_seconds=0.01, heartbeat_seconds=1,
    )
    assert await anext(stream) == "retry: 3000\n\n"
    next_frame = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.02)
    assert not next_frame.done()

    service.append(5)
    frame = await asyncio.wait_for(next_frame, timeout=1)
    assert frame.startswith("id: 5\n")
    assert json.loads(frame.split("data: ", 1)[1])["sequence"] == 5
    service.task = service.task.model_copy(update={"state": TaskState.COMPLETED})
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1)


@pytest.mark.asyncio
async def test_sse_stops_on_client_disconnect() -> None:
    service = EventService(state=TaskState.PLANNING)
    request = ConnectedRequest()
    stream = stream_task_events(request, service, service.task.task_id, after_sequence=0)
    assert await anext(stream) == "retry: 3000\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_sse_keeps_needs_human_room_open_for_agent_chat() -> None:
    service = EventService(state=TaskState.NEEDS_HUMAN)
    request = ConnectedRequest()
    stream = stream_task_events(
        request, service, service.task.task_id,
        after_sequence=0, poll_seconds=0.01, heartbeat_seconds=1,
    )
    assert await anext(stream) == "retry: 3000\n\n"
    next_frame = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.02)
    assert not next_frame.done()
    service.append(1, content="discussion reply")
    assert (await asyncio.wait_for(next_frame, timeout=1)).startswith("id: 1\n")
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
