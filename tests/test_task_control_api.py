"""Read-only UI control snapshot backed by real task/claim/budget stores."""

from uuid import uuid4

import pytest

from app.team.budgets import ConversationBudgetPolicy
from tests.test_continuation_execution import client, finished
from tests.test_continuation_workflow import human_command

pytest_plugins = ("tests.test_continuation_workflow",)


@pytest.mark.asyncio
async def test_control_snapshot_tracks_budget_and_latest_workflow(waiting_for_planner):
    service, view, _ = waiting_for_planner
    url = f"/api/v1/tasks/{view.task_id}/control"
    async with client(service) as api:
        initial = await api.get(url)
        assert initial.status_code == 200, initial.text
        control = initial.json()
        assert control["task_state"] == "needs_human"
        assert control["task_revision"] == view.revision
        assert control["runtime_revision"] >= 1
        assert control["rework_rounds"] == 0
        assert control["max_rework_rounds"] == 2
        assert control["budget_usage"]["agent_turns"] == 1
        assert control["budget_policy"]["max_agent_turns"] > 1
        assert control["budget_violation"] is None
        assert control["latest_continuation"] is None
        assert control["latest_workflow_outcome"] is None
        assert "claim_token" not in initial.text

        body = await human_command(api, view)
        messages = (await api.get(f"/api/v1/tasks/{view.task_id}/messages")).json()["items"]
        selected = next(item for item in messages if item["message_id"] == body["message_id"])
        assert selected["pending_for_continuation"] is True
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        await finished(service, request_id)
        updated_messages = (await api.get(f"/api/v1/tasks/{view.task_id}/messages")).json()["items"]
        consumed = next(item for item in updated_messages if item["message_id"] == body["message_id"])
        assert consumed["pending_for_continuation"] is False
        final = await api.get(url)
        assert final.status_code == 200, final.text
        control = final.json()
        assert control["latest_continuation"]["receipt"]["request"]["request_id"] == request_id
        assert control["latest_continuation"]["receipt"]["state"] == "succeeded"
        assert control["latest_workflow_outcome"]["success"] is True
        assert control["latest_workflow_outcome"]["final_state"] == "completed"
        assert control["budget_usage"]["agent_turns"] >= 2
        assert "claim_token" not in final.text


@pytest.mark.asyncio
async def test_control_snapshot_reports_budget_block_without_granting_authority(waiting_for_planner):
    service, view, _ = waiting_for_planner
    service.event_loop.executor.budget_guard.policy = ConversationBudgetPolicy(max_agent_turns=0)
    async with client(service) as api:
        response = await api.get(f"/api/v1/tasks/{view.task_id}/control")
        assert response.status_code == 200
        assert response.json()["budget_violation"]["code"] == "agent_turns"
        assert response.json()["budget_usage"]["agent_turns"] == 1
        assert (await api.get(f"/api/v1/tasks/{uuid4()}/control")).status_code == 404
