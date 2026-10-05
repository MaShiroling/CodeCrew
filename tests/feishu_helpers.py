"""Fake-only harness: real stores/dispatcher/runtime, no CLI or OS sandbox emulation."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.agents import ChatExecutionWorkspace, StandaloneChatAgentRuntime
from app.chat.bounded_dispatch import BoundedDiscussionDispatcher
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.config import Settings
from app.feishu.bridge import FeishuBridge
from app.feishu.models import FeishuInbound
from app.feishu.outbox import FeishuOutbox
from app.feishu.runtime import FeishuRuntime
from app.feishu.sender import FakeFeishuSender
from app.feishu.store import FeishuStore
from app.feishu.transport import FakeFeishuTransport
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def reply(content="讨论结束", action="finish", targets=()):
    return FakeAgentScenario(output={"structured_output": {
        "content": content, "next_action": action, "handoff_to": list(targets),
    }})


class SequenceAgent(FakeAgentAdapter):
    def __init__(self, *scenarios):
        super().__init__(scenarios[0])
        self.scenarios = scenarios

    async def start(self, request):
        self._scenario = self.scenarios[min(len(self.requests), len(self.scenarios) - 1)]
        return await super().start(request)


class FakeOnlyWorkspaces:
    """No real Agent may use this. Production permission checks remain intact."""

    def __init__(self, root: Path):
        self.root = root

    def create(self, room_id):
        execution_id = uuid4()
        path, runtime = self.root / str(execution_id), self.root / (str(execution_id) + '-runtime')
        path.mkdir(parents=True)
        runtime.mkdir()
        return ChatExecutionWorkspace(execution_id, room_id, path, runtime)

    def cleanup(self, workspace):
        workspace.path.rmdir()
        workspace.runtime.rmdir()


def setup(tmp_path, *, adapters=None, failures=0, **overrides):
    settings = Settings(_env_file=None, **{
        "feishu_enabled": True, "feishu_app_id": "cli_test", "feishu_app_secret": "fake-secret-unique",
        "feishu_bot_open_id": "ou_bot", "feishu_allowed_chat_ids": frozenset({"oc_dm", "oc_group"}),
        "feishu_allowed_sender_open_ids": frozenset({"ou_alice", "ou_bob"}),
        **overrides,
    })
    chat = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    chat.initialize()
    service = StandaloneChatService(chat)
    adapters = adapters or {
        MemberRole.PLANNER: SequenceAgent(reply("白金：第一条", "handoff", ("implementer",)), reply("第三条")),
        MemberRole.IMPLEMENTER: SequenceAgent(reply("第二条", "handoff", ("planner",))),
        MemberRole.REVIEWER: SequenceAgent(reply("审阅结束")),
    }
    assert all(isinstance(agent, FakeAgentAdapter) for agent in adapters.values())
    agents = StandaloneChatAgentRuntime(FakeOnlyWorkspaces(tmp_path / "fake"), adapters)
    dispatcher = BoundedDiscussionDispatcher(chat, agents)
    store = FeishuStore(chat, settings.feishu_app_id)
    store.initialize()
    bridge = FeishuBridge(service, dispatcher, store, settings)
    sender, transport = FakeFeishuSender(failures=failures), FakeFeishuTransport()
    outbox = FeishuOutbox(store, sender, settings)
    runtime = FeishuRuntime(bridge, outbox, transport)
    return SimpleNamespace(**locals())


def inbound(number=1, **overrides):
    return FeishuInbound(**{
        "app_id": "cli_test", "event_id": f"ev_{number}", "message_id": f"om_{number}",
        "chat_id": "oc_dm", "chat_type": "p2p", "sender_open_id": "ou_alice",
        "text": "讨论方案", "mentions_bot": False, **overrides,
    })


def rows(h, table):
    assert table in {"feishu_outbox", "feishu_ingress", "feishu_ingress_events", "feishu_bindings"}
    with h.store.database.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def requests(h):
    return [request for agent in h.adapters.values() for request in agent.requests]


def release_retry(h):
    with h.store.database.transaction() as conn:
        conn.execute("UPDATE feishu_outbox SET next_retry_at='2000-01-01' WHERE status='retry_wait'")
