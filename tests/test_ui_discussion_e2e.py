"""8.4: one disposable HTTP/UI/SQLite/Fake-Agent conversation, without model calls."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.orchestration.models import TaskState
from app.team import MemberRole, MessageType
from tests.test_discussion_dispatch import attach_runner, discussion_output

pytest_plugins = ("tests.test_continuation_workflow",)


@pytest.mark.asyncio
async def test_ui_served_and_three_fake_agents_chat_without_coding(waiting_for_planner):
    service, task, _workflow_agents = waiting_for_planner
    room = (await service.get_room(task.task_id)).room
    adapters = attach_runner(service, {
        MemberRole.PLANNER: discussion_output("human", "implementer"),
        MemberRole.IMPLEMENTER: discussion_output("human"),
        MemberRole.REVIEWER: discussion_output("human"),
    })
    original_task = await service.get_task(task.task_id)
    original_runtime = service.contexts.get(task.task_id)
    app = create_app(task_service=service)
    base = f"/api/v1/tasks/{task.task_id}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/ui/")
        script = await client.get("/ui/assets/app.js")
        assert page.status_code == script.status_code == 200
        assert 'id="discussion-form"' in page.text
        assert 'data-discussion-mention="@白金"' in page.text
        assert "/messages/discussion" in script.text
        assert (await client.get(base)).json()["state"] == "needs_human"
        control = await client.get(f"{base}/control")
        assert control.status_code == 200, control.text

        first = await client.post(f"{base}/messages/discussion", json={
            "expected_revision": task.revision,
            "idempotency_key": str(uuid4()),
            "content": "@白金 @鲸鲸 请和月见讨论这个修复方案，不要改代码",
        })
        assert first.status_code == 201, first.text
        receipt = first.json()
        assert receipt["scope"] == "discussion"
        assert receipt["discussion_queued"] is True
        assert receipt["execution_authorized"] is False
        assert receipt["agent_dispatched"] is False
        await asyncio.wait_for(service.wait_for_discussion(task.task_id), timeout=3)

        messages = (await client.get(f"{base}/messages")).json()["items"]
        thread = [message for message in messages
                  if message["correlation_id"] == receipt["message"]["correlation_id"]]
        assert [message["sender_role"] for message in thread] == [
            "human", "planner", "planner", "reviewer", "implementer",
        ]
        planner = next(message for message in thread if message["sender_role"] == "planner")
        followup = await client.post(f"{base}/messages/discussion", json={
            "expected_revision": task.revision,
            "idempotency_key": str(uuid4()),
            "content": "请继续说明测试边界",
            "reply_to": planner["message_id"],
        })
        assert followup.status_code == 201, followup.text
        assert followup.json()["message"]["reply_to"] == planner["message_id"]
        assert followup.json()["message"]["correlation_id"] == planner["correlation_id"]
        await asyncio.wait_for(service.wait_for_discussion(task.task_id), timeout=3)

        after = (await client.get(f"{base}/messages")).json()["items"]
        assert len([message for message in after
                    if message["correlation_id"] == planner["correlation_id"]]) > len(thread)
        assert (await client.get(base)).json()["state"] == "needs_human"

    assert service.contexts.get(task.task_id) == original_runtime
    assert await service.get_task(task.task_id) == original_task
    assert not service._runs
    assert all(request.discussion_only and request.permission_mode.value == "read_only"
               for adapter in adapters.values() for request in adapter.requests)
    assert all(adapter.requests for adapter in adapters.values())
    assert not any(stored.message.type in {MessageType.IMPLEMENTATION_READY,
                                           MessageType.REVIEW_APPROVED}
                   for stored in service.rooms.list_messages(room.room_id))
    assert (await service.get_task(task.task_id)).state is TaskState.NEEDS_HUMAN
