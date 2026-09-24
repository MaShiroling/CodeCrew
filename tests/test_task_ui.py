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
        assert 'src="/ui/assets/app.js"' in page.text
        assert client.get("/ui").status_code == 200

        javascript = client.get("/ui/assets/app.js")
        assert javascript.status_code == 200
        assert "/api/v1" in javascript.text
        assert "textContent" in javascript.text

        stylesheet = client.get("/ui/assets/styles.css")
        assert stylesheet.status_code == 200
        assert "workspace-grid" in stylesheet.text
        assert client.get("/ui/assets/missing.js").status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/tasks").status_code == 503


def test_live_ui_state_transitions_with_mock_eventsource() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_live.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)
