import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_task_ui_serves_local_assets_without_task_runtime() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/ui/")
        assert page.status_code == 200
        assert "开发任务工作台" in page.text
        assert 'id="create-form"' in page.text
        assert 'id="create-repository"' in page.text
        assert 'id="create-issue"' in page.text
        assert 'id="cancel-task"' in page.text
        assert 'id="human-form"' in page.text
        assert 'id="human-reply"' in page.text
        assert 'id="control-panel"' in page.text
        assert 'id="overview-phase"' in page.text
        assert 'id="overview-agent"' in page.text
        assert 'id="overview-blocking"' in page.text
        assert 'id="overview-recent"' in page.text
        assert 'id="continue-workflow"' in page.text
        assert 'id="cancel-continuation"' in page.text
        assert 'class="topbar-left"' in page.text
        assert '<details id="issue-details"' in page.text
        assert 'class="inspector-column"' in page.text
        assert page.text.index('id="message-list"') < page.text.index('id="control-panel"')
        assert 'class="sidebar"' not in page.text
        assert 'src="/ui/assets/app.js"' in page.text
        assert 'href="/ui/assets/layout.css"' in page.text
        assert client.get("/ui").status_code == 200

        javascript = client.get("/ui/assets/app.js")
        assert javascript.status_code == 200
        assert "/api/v1" in javascript.text
        assert "textContent" in javascript.text

        stylesheet = client.get("/ui/assets/styles.css")
        assert stylesheet.status_code == 200
        assert "workspace-grid" in stylesheet.text
        layout = client.get("/ui/assets/layout.css")
        assert layout.status_code == 200
        assert "grid-template-columns: minmax(218px, 250px)" in layout.text
        assert "@media (max-width: 760px)" in layout.text
        assert client.get("/ui/assets/missing.js").status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/tasks").status_code == 503


def test_live_ui_state_transitions_with_mock_eventsource() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_live.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)


def test_ui_create_task_with_mock_api() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_create.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)


def test_ui_cancel_task_with_mock_api() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_cancel.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)


def test_ui_human_message_and_reply_with_mock_api() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_human.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)


def test_ui_controlled_workflow_with_mock_api() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_control.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)
