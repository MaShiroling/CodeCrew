"""Start a disposable local UI/API smoke server without real Agent model calls.

Run from the repository root: .venv/bin/python tests/manual_ui_smoke.py
The printed repository path exists only until this process exits.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from test_persistent_task_service import make_repository, make_service

from app.main import create_app


def main() -> None:
    with TemporaryDirectory(prefix="codecrew-ui-smoke-") as directory:
        root = Path(directory)
        repository = make_repository(root)
        service, _controlled_loop = make_service(root)
        print(f"Temporary Git repository: {repository}", flush=True)
        print("Open http://127.0.0.1:8765/ui/; create a task, then cancel it.", flush=True)
        print("This controlled workflow calls no real Agent or model.", flush=True)
        uvicorn.run(create_app(task_service=service), host="127.0.0.1", port=8765, workers=1)


if __name__ == "__main__":
    main()
