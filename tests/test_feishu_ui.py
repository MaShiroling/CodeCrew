import shutil
import subprocess
from pathlib import Path

import pytest


def test_feishu_browser_identity_and_status():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the UI test")
    result = subprocess.run([node, str(Path(__file__).with_name("ui_feishu.test.cjs"))],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
