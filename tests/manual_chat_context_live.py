"""Disposable browser fixture for one real Codex Planner context turn.

Run from the project root: .venv/bin/python tests/manual_chat_context_live.py
This seeds Human messages without dispatching them; only the message sent in the
browser spends a model turn. Ctrl-C removes the temporary SQLite room.
"""

import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import uvicorn

from app.cli import build_chat_app
from app.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    installed = Settings()
    if shutil.which(installed.codex_cli_path) is None:
        parser.error("Codex CLI unavailable; set CODECREW_CODEX_CLI_PATH first")
    with TemporaryDirectory(prefix="codecrew-context-browser-") as directory:
        root = Path(directory).resolve()  # macOS /var is a symlink to /private/var.
        settings = installed.model_copy(update={
            "database_url": f"sqlite:///{root / 'chat.sqlite3'}",
            "standalone_chat_workspace_root": root / "workspaces",
            "standalone_chat_runtime_root": root / "runtime",
        })
        app = build_chat_app(settings=settings)
        app.state.chat_dispatcher.max_turns_per_thread = 1
        service = app.state.chat_service
        room = service.create_room(title="P2.4 真实背景验收", idempotency_key=uuid4())
        service.post_message(
            room.room_id,
            content="@白金 原始目标：讨论输入验证方案；验收代号 ANCHOR_BLUE_73。只讨论，不改代码。",
            idempotency_key=uuid4(), reply_to=None,
        )
        for index in range(8):
            service.post_message(
                room.room_id, content=f"@白金 无关旧话题 {index} " + "x" * 250,
                idempotency_key=uuid4(), reply_to=None,
            )
        print(f"Open http://127.0.0.1:{args.port}/ui/chat/?room={room.room_id}", flush=True)
        print("Click the FIRST Human message's '以此为背景继续 ↗' button.", flush=True)
        print("Send: @白金 请只回答选定背景中的验收代号；不要读写文件或邀请队友。", flush=True)
        print("Only this browser send calls a real model. Ctrl-C removes the fixture.", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
