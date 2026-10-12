"""No-key, scripted browser journey through the production memory boundaries.

This is a Fake fixture, not an extraction-quality evaluation. Its private data
directory survives shutdown so the same database can be checked after restart.
"""

import argparse
import json
import re
from pathlib import Path

import uvicorn

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team.models import MemberRole

PREFERENCE = "以后在本房间请用中文简洁回答，先给结论。"
TODO = "下次发布前检查文档链接。"
COMPLETE = "文档链接检查已经完成。"
CANCEL = "文档链接检查这项待办已取消。"
_MARKER = "codecrew-memory-browser-demo-v1\n"


class MemoryDemoAdapter(FakeAgentAdapter):
    """Only fixed synthetic source lines produce proposals; reads use real recall."""

    async def start(self, request):
        prefix = "上下文摘录（JSON）："
        line = next(line for line in request.prompt.splitlines() if line.startswith(prefix))
        context = json.loads(line[len(prefix):])
        received = request.prompt.rsplit("收到的消息：", 1)[1].strip()
        source = re.sub(r"^@\S+\s+", "", received)
        memories = context["room_memories"]
        payload = {
            "content": "[Fake 记忆演示] 本次没有召回活跃长期记忆。",
            "handoff_to": [], "memory_proposal": None, "memory_action": None,
        }
        if source in {PREFERENCE, TODO}:
            payload["memory_proposal"] = {
                "kind": "preference" if source == PREFERENCE else "open_loop",
                "content": source,
            }
            payload["content"] = (
                "[Fake 记忆演示] 已提交原文提议；是否落库请查看右侧长期记忆。"
            )
        elif source in {COMPLETE, CANCEL}:
            targets = [item for item in memories
                       if item["kind"] == "open_loop" and item["content"] == TODO]
            if len(targets) == 1:
                payload["memory_action"] = {
                    "action": "complete" if source == COMPLETE else "cancel",
                    "memory_id": targets[0]["memory_id"], "evidence": source,
                }
        elif memories:
            recalled = "；".join(
                f"{item['content']}（来源消息 {item['source_message_id'][:8]}）"
                for item in memories
            )
            payload["content"] = f"[Fake 记忆演示] 本次实际召回：{recalled}"
        self._scenario = FakeAgentScenario(output={
            "message": json.dumps(payload, ensure_ascii=False),
        })
        return await super().start(request)


def build_memory_demo_app(data_dir: Path):
    """Never load Settings/.env, construct a cloud embedder, or seed memory rows."""
    root = data_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / ".memory-browser-demo"
    if marker.exists():
        if marker.read_text(encoding="utf-8") != _MARKER:
            raise ValueError("not a recognized memory demo directory")
    elif any(root.iterdir()):
        raise ValueError("use a new empty private directory, not existing project data")
    else:
        marker.write_text(_MARKER, encoding="utf-8")
    workspaces = StandaloneChatWorkspaceManager(root / "workspaces", root / "runtime")
    store = StandaloneChatStore(SQLiteDatabase(root / "chat.sqlite3"))
    store.initialize()
    runtime = StandaloneChatAgentRuntime(workspaces, {
        role: MemoryDemoAdapter()
        for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)
    })
    return create_app(
        chat_service=StandaloneChatService(store),
        chat_dispatcher=StandaloneChatDispatcher(store, runtime),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="new empty private directory, or the same demo dir after restart")
    parser.add_argument("--port", type=int, default=8772)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        app = build_memory_demo_app(args.data_dir)
    except ValueError as exc:
        parser.error(str(exc))
    print("Fake 记忆演示：无真实模型、无 embedding、无编码/飞书入口。", flush=True)
    print(f"演示数据：{args.data_dir.resolve()}（停止后保留，可用相同目录重启）", flush=True)
    print(f"打开 http://127.0.0.1:{args.port}/ui/chat/", flush=True)
    print(f"来源示例：@白金 {PREFERENCE}", flush=True)
    print(f"待办示例：@白金 {TODO}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
