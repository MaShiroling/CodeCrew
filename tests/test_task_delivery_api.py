"""Read-only delivery evidence and task-scoped Patch download."""

import hashlib
from uuid import UUID, uuid4

import pytest

from app.storage import ArtifactType
from tests.test_continuation_execution import client, finished
from tests.test_continuation_workflow import human_command

pytest_plugins = ("tests.test_continuation_workflow",)


@pytest.mark.asyncio
async def test_delivery_is_empty_before_verifier_and_rejects_missing_task(waiting_for_planner):
    service, view, _ = waiting_for_planner
    async with client(service) as api:
        delivery = await api.get(f"/api/v1/tasks/{view.task_id}/delivery")
        assert delivery.status_code == 200, delivery.text
        body = delivery.json()
        assert body["task_state"] == "needs_human"
        assert body["task_revision"] == view.revision
        assert body["delivery_ready"] is False
        assert all(body[key] is None for key in ("verification", "review", "completion", "patch"))
        assert (await api.get(f"/api/v1/tasks/{uuid4()}/delivery")).status_code == 404
        assert (await api.get(
            f"/api/v1/tasks/{view.task_id}/delivery/patch/{uuid4()}"
        )).status_code == 404


@pytest.mark.asyncio
async def test_delivery_summarizes_bound_evidence_and_downloads_exact_patch(waiting_for_planner):
    service, view, _ = waiting_for_planner
    async with client(service) as api:
        command = await human_command(api, view)
        accepted = await api.post(
            f"/api/v1/tasks/{view.task_id}/continue/workflow", json=command,
        )
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        await finished(service, request_id)

        delivery = await api.get(f"/api/v1/tasks/{view.task_id}/delivery")
        assert delivery.status_code == 200, delivery.text
        body = delivery.json()
        assert body["task_state"] == "completed"
        assert body["delivery_ready"] is True
        verification = body["verification"]
        assert verification["passed"] is True
        assert "src/app.py" in verification["changed_files"]
        by_kind = {check["kind"]: check for check in verification["checks"]}
        assert by_kind["static_analysis"]["status"] == "passed"
        assert by_kind["public_tests"]["status"] == "passed"
        assert by_kind["hidden_tests"]["status"] == "passed"
        assert by_kind["hidden_tests"]["detail"] is None
        assert body["review"]["verdict"] == "approved"
        assert body["review"]["follows_latest_verification"] is True
        assert body["completion"]["passed"] is True
        assert all(item["passed"] for item in body["completion"]["conditions"])
        patch = body["patch"]
        patch_url = f"/api/v1/tasks/{view.task_id}/delivery/patch/{patch['artifact_id']}"
        downloaded = await api.get(patch_url)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.headers["content-type"].startswith("text/x-diff")
        assert "attachment" in downloaded.headers["content-disposition"]
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-artifact-sha256"] == patch["sha256"]
        assert hashlib.sha256(downloaded.content).hexdigest() == patch["sha256"]
        assert b"value = 2" in downloaded.content
        assert (await api.get(
            f"/api/v1/tasks/{uuid4()}/delivery/patch/{patch['artifact_id']}"
        )).status_code == 404
        assert (await api.get(
            f"/api/v1/tasks/{view.task_id}/delivery/patch/{uuid4()}"
        )).status_code == 404
        unrelated = service.router.artifacts.put_bytes(
            b"unrelated patch", task_id=view.task_id, trace_id=view.trace_id,
            type=ArtifactType.DIFF, media_type="text/x-diff", created_by="test",
        )
        assert (await api.get(
            f"/api/v1/tasks/{view.task_id}/delivery/patch/{unrelated.artifact_id}"
        )).status_code == 404

        blob = service.router.artifacts.blob_path_for(UUID(patch["artifact_id"]))
        blob.write_bytes(b"tampered test patch")
        corrupted = await api.get(patch_url)
        assert corrupted.status_code == 500
        assert corrupted.json()["error"]["code"] == "task_artifact_integrity_error"
