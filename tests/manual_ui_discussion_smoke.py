"""Disposable three-Fake-Agent chat room for a manual browser check.

Run from the repository root: .venv/bin/python tests/manual_ui_discussion_smoke.py
No model, API key, or code-writing Agent is used. Ctrl-C removes the fixture.
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from app.agents import FakeAgentScenario
from app.api.models import CreateTaskRequest
from app.main import create_app
from app.orchestration.models import TaskState
from app.team import MemberRole
from tests.test_discussion_dispatch import attach_runner, discussion_output
from tests.test_task_api_e2e import make_repository, make_runtime


async def prepare(root: Path):
    repository = make_repository(root)
    runtime, workflow_agents = make_runtime(root, edit_code=False)
    workflow_agents[0]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_human_input", "recipient": {"kind": "role", "role": "human"},
         "content": "请先讨论方案，不要开始修改代码。"},
        {"action": "finish_turn", "content": "Waiting for discussion"},
    ]})
    service = runtime.service
    created = await service.create_task(CreateTaskRequest(
        issue="请三位 Agent 讨论一个小型修复方案；这只是只读聊天演示，不执行编码。",
        repository_path=str(repository),
    ))
    await asyncio.wait_for(service.wait_for(created.task_id), timeout=10)
    assert (await service.get_task(created.task_id)).state is TaskState.NEEDS_HUMAN
    attach_runner(service, {
        MemberRole.PLANNER: discussion_output("human", "implementer"),
        MemberRole.IMPLEMENTER: discussion_output("human"),
        MemberRole.REVIEWER: discussion_output("human"),
    })
    return runtime, created.task_id


def main() -> None:
    with TemporaryDirectory(prefix="codecrew-chat-smoke-") as directory:
        runtime, task_id = asyncio.run(prepare(Path(directory)))
        print(f"Fake team task: {task_id}", flush=True)
        print("Open http://127.0.0.1:8766/ui/", flush=True)
        print("Send: @白金 @鲸鲸 请一起讨论方案", flush=True)
        print("No real model calls or code changes; Ctrl-C removes this fixture.", flush=True)
        uvicorn.run(create_app(runtime=runtime), host="127.0.0.1", port=8766, workers=1)


if __name__ == "__main__":
    main()
