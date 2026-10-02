# CodeCrew｜多 Agent 协作聊天室与受控编码

CodeCrew 是一个本地运行的个人开源项目：你可以在独立聊天室里与白金（Codex）、月见（Kimi）和鲸鲸（DeepSeek，经 Claude Code CLI）讨论方案；只有 Human 明确选择 Git 仓库、通过预检并授权一次任务后，团队才进入修改代码的工作流。聊天消息本身不授予写权限。

项目借鉴 [Clowder AI](https://github.com/MaShiroling/clowder-ai) 的异构 Agent 协作思路，但代码、Prompt 和 UI 独立实现，聚焦“聊天协作 + 按需编码”，不追求通用聊天平台或复刻猫咖全部功能。

## 现在能演示什么

| 场景 | 已验证范围 |
| --- | --- |
| 无仓库聊天室 | 创建房间、`@` 成员、关联回复、Agent 间邀请接话、消息持久化与实时状态；Fake 浏览器演示和真实三模型最小对话已验收。 |
| Human 授权后改代码 | 只读预检 → 单次授权 → 独立 Worktree → Verifier → Reviewer → CompletionGuard → Patch；Fake 浏览器闭环已验收。 |
| 真实三 Agent 小修复 | 临时仓库的单次 HTTP 自动验收已通过：唯一变更 `src/app.py`，检查、审批、完成守卫与 Patch 均通过。真实浏览器手动代码演示仍待复测。 |

CodeCrew 不把 Agent 的“已完成”当作成功证据。一次通过也**不是**任意任务成功率或正式可靠性评测。[当前路线与逐步进度](docs/portfolio-roadmap.md)记录了每项能力的验收边界。

## 5 分钟体验：无需密钥

需要 Python 3.11+ 和 Git。在终端依次运行：

```bash
git clone https://github.com/MaShiroling/CodeCrew.git
cd CodeCrew
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.cli chat-demo --port 8000
```

打开 [http://127.0.0.1:8000/ui/chat/](http://127.0.0.1:8000/ui/chat/)，创建房间并发送 `@白金 请和月见、鲸鲸讨论输入校验方案；只讨论，不修改文件`。三位 Fake Agent 会接话；可继续回复并刷新查看历史。此入口不调用真实模型，也不创建编码任务。`chat-demo` 的 SQLite 数据默认保存在本地 `./codecrew.db`，退出服务不会自动清空。
[完整现场步骤与 Fake 演示截图](docs/demo-guide.md)将只读聊天、受控编码和真实模型入口分开说明。

![CodeCrew Fake 演示聊天室](docs/media/chat-discussion.jpg)

想看“聊天后由 Human 授权一次修复”的完整 Fake 流程，先停止上一个服务，再运行：

```bash
.venv/bin/python -m app.cli demo-serve --port 8000
```

同样打开 `/ui/chat/`，创建房间并发送 `@白金 请讨论只修改 src/app.py，把 value 从 1 改为 2`。等团队回复后，在该 Human 消息旁选择“以此发起受控编码任务”，核对预检结果，勾选确认并授权；任务工作台会展示 Diff、验证结果和 Patch。此演示只接受临时样例仓库和固定修改，关闭服务后临时数据会删除。[图文操作与边界](docs/fake-chat-to-code-demo.md)

## 真实模型如何运行

只读聊天室使用 `chat-serve`；CLI 与密钥由本机环境提供，不写入仓库。真实 Codex、Kimi、DeepSeek 的安装和凭据步骤见[独立聊天室在线验收](docs/standalone-chat-live.md)。真实模型从聊天到一次小修复的受控命令、测试和安全说明见[P3.5 验收指南](docs/real-chat-to-code-acceptance.md)。首次体验建议先用上面的 Fake 演示，不需要 API Key。

2026-10-02 的一次真实小修复验收：任务 `faac33fe-5198-42e0-bbb5-d453eb9a1a0b`，trace `9b01b794-65fb-428a-9bd3-53326a859468`，状态 `completed`；Planner、Implementer、Reviewer 均参与，确定性检查与完成守卫通过。摘要存于本机被 Git 忽略的 `evals/results/chat-to-code-live/`，不是完整原始运行归档。[证据范围与限制](docs/real-chat-to-code-acceptance.md)

## 设计概览

- 独立聊天室：SQLite 持久化房间、消息和回合；`@` 路由与有界上下文支持成员接话，默认只读。
- 受控编码：Human 的预检与单次授权绑定消息、仓库、Git 基线和允许路径；Implementer 在独立 Git Worktree 工作。
- 证据判定：Verifier 执行静态/编译、公开和占位隐藏检查、路径与命令审计；独立 Reviewer 只读审批，CompletionGuard 复核必要条件并输出 Patch。
- 可追溯性：结构化 A2A 消息、Artifact 引用、`trace_id` 与持久化事件支持检查和故障归因。

技术栈：Python 3.11+、FastAPI、Pydantic、asyncio、SQLite、Git Worktree、SSE、pytest。[当前架构图与模块边界](docs/architecture.md)分别展示只读聊天和授权后的受控编码链路。

## 验证与边界

```bash
.venv/bin/pytest -q tests/test_standalone_chat_api.py tests/test_demo.py tests/test_chat_to_code_live_fixture.py
.venv/bin/ruff check .
```

真实模型集成测试默认跳过，只有显式设置对应 `CODECREW_RUN_*_LIVE=1` 才会调用模型并消耗额度。项目仅面向可信本机：尚无公网身份认证或通用不可信代码沙箱；示例“隐藏测试”不是对本机操作者保密的评测环境。正式多语言评测集、单/多 Agent 对照实验和可靠性指标报告均未完成，不作为本版已交付能力。[更多现状](docs/project-status.md)

## 文档

- [本地演示指南与截图](docs/demo-guide.md) · [当前架构图](docs/architecture.md)
- [当前路线和进度](docs/portfolio-roadmap.md) · [项目状态与验收记录](docs/project-status.md)
- [Fake 聊天到编码演示](docs/fake-chat-to-code-demo.md) · [真实小修复验收](docs/real-chat-to-code-acceptance.md)
- [独立聊天室接口](docs/standalone-chat.md) · [团队人格](docs/personas.md)
- [旧版 README 历史快照](docs/readme-history.md) · [旧 15 步开发计划](docs/development-plan.md)

## License

Apache-2.0
