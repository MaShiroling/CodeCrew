from uuid import UUID

from fastapi.testclient import TestClient

from app.api.models import CancelTaskRequest, CreateTaskRequest, TaskPage, TaskView
from app.api.service import TaskNotFound, TaskStateConflict
from app.main import create_app
from app.orchestration.models import Task, TaskState


class FakeTaskService:
    def __init__(self) -> None:
        self.tasks: dict[UUID, TaskView] = {}

    async def create_task(self, request: CreateTaskRequest) -> TaskView:
        task = Task(issue=request.issue, repository_path=request.repository_path)
        view = self._view(task, revision=1)
        self.tasks[task.id] = view
        return view

    async def get_task(self, task_id: UUID) -> TaskView:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise TaskNotFound(f"task {task_id} does not exist") from exc

    async def list_tasks(
        self, *, state: TaskState | None, limit: int, offset: int
    ) -> TaskPage:
        filtered = [view for view in self.tasks.values() if state is None or view.state is state]
        page = filtered[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(filtered) else None
        return TaskPage(
            items=tuple(page), limit=limit, offset=offset, next_offset=next_offset
        )

    async def cancel_task(
        self, task_id: UUID, request: CancelTaskRequest
    ) -> TaskView:
        current = await self.get_task(task_id)
        if current.revision != request.expected_revision:
            raise TaskStateConflict("task revision changed")
        if current.state is not TaskState.CREATED:
            raise TaskStateConflict("task can no longer be cancelled")
        updated = current.model_copy(
            update={"state": TaskState.CANCELLED, "revision": current.revision + 1}
        )
        self.tasks[task_id] = updated
        return updated

    @staticmethod
    def _view(task: Task, *, revision: int) -> TaskView:
        return TaskView(
            task_id=task.id,
            trace_id=task.trace_id,
            issue=task.issue,
            repository_path=task.repository_path,
            state=task.state,
            rework_rounds=task.rework_rounds,
            revision=revision,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


def test_task_routes_have_explicit_openapi_contract() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert "post" in paths["/api/v1/tasks"]
    assert "get" in paths["/api/v1/tasks"]
    assert "get" in paths["/api/v1/tasks/{task_id}"]
    assert "post" in paths["/api/v1/tasks/{task_id}/cancel"]
    assert paths["/api/v1/tasks"]["post"]["responses"]["201"]
    assert paths["/api/v1/tasks/{task_id}/cancel"]["post"]["requestBody"]["required"]


def test_task_contract_create_list_get_and_cancel() -> None:
    client = TestClient(create_app(task_service=FakeTaskService()))

    created = client.post(
        "/api/v1/tasks",
        json={"issue": "Fix parser", "repository_path": "/tmp/codecrew-fixture"},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]
    assert created.json()["state"] == "created"
    assert created.json()["revision"] == 1

    listed = client.get("/api/v1/tasks", params={"state": "created", "limit": 1})
    assert listed.status_code == 200
    assert [item["task_id"] for item in listed.json()["items"]] == [task_id]
    assert client.get(f"/api/v1/tasks/{task_id}").json() == created.json()

    cancelled = client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        json={"expected_revision": 1, "reason": "No longer needed"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["revision"] == 2

    stale = client.post(
        f"/api/v1/tasks/{task_id}/cancel", json={"expected_revision": 1}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "task_state_conflict"


def test_task_contract_validation_and_unavailable_service() -> None:
    client = TestClient(create_app(task_service=FakeTaskService()))
    invalid = client.post(
        "/api/v1/tasks", json={"issue": "   ", "repository_path": "/tmp/repo"}
    )
    unavailable = TestClient(create_app()).post(
        "/api/v1/tasks", json={"issue": "Fix bug", "repository_path": "/tmp/repo"}
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert invalid.json()["error"]["issues"]
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {
            "code": "task_service_unavailable",
            "message": "task workflow service is not configured",
        }
    }


def test_task_contract_missing_task_returns_structured_404() -> None:
    client = TestClient(create_app(task_service=FakeTaskService()))

    response = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000001")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"
