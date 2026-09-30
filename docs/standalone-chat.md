# 独立聊天室数据契约、HTTP API 与 UI

8.4a 提供领域模型和 SQLite 存储；8.4b 开放本地 HTTP API，8.4d 已接入只读 Agent 调度，
P1 已提供独立聊天 UI。创建聊天室
不会调用 `PersistentTaskService.create_task`，不需要用户 Git 路径，也不会创建 Worktree、
调用 Verifier 或进入 CompletionGuard。后续功能必须显式接入，不能把旧任务聊天室
视作独立入口。

## 持久化边界

- `StandaloneChatRoom` 有独立 `room_id`、`trace_id`、标题、开关状态和四位成员：
  Human、Planner、Implementer、Reviewer。它没有 `task_id` 或 `repository_path`。
- `StandaloneChatMessage` 记录发送者、1～3 位具体收件成员、文本、关联回复、
  `correlation_id`、`causation_id` 和幂等键；不承载计划、代码、审批或完成动作。
- Migration 15 新建 `standalone_chat_*` 四张表，保留既有任务表及其迁移不变。
  同一发送者在同一房间复用幂等键时，只允许语义相同的重放。Migration 16 新增
  逐投递 Agent 回合状态，不改变原任务表。
- 投递状态按收件人独立保存；ACK 幂等。回复和因果引用必须留在同一房间及
  同一 `correlation_id`，关闭后不接受新消息，但允许读取与相同请求重放。

## 本地 HTTP 接口（8.4b）

在项目根目录运行 `.venv/bin/python -m app.cli chat-serve --port 8000`，启动时无需 Git 仓库、
验证计划、Agent CLI 或模型密钥；但发送 @消息会尝试启动所提及的真实 Agent，
因此相应 CLI、凭据和平台边界届时必须可用。访问 `http://127.0.0.1:8000/docs` 可试用 HTTP API。
现有 `codecrew serve` 也会挂载同一组路由，但仍按编码任务配置启动。`/ui/`
仍是任务工作台；**独立聊天室入口为 `/ui/chat/`**。若不想调用真实模型，运行
`.venv/bin/python -m app.cli chat-demo --port 8000`，它用三个 Fake Agent 展示完整
只读接话，并把消息持久化到配置的 SQLite。路由和
`/api/v1/tasks` 是不同资源；没有 `repository_path`、Task ID 或执行授权字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/chats` | 创建房间；请求含 `title` 和 UUID `idempotency_key` |
| `GET` | `/api/v1/chats?limit=50&offset=0` | 列表，按创建时间倒序 |
| `GET` | `/api/v1/chats/{room_id}` | 房间详情与四位成员 |
| `GET` | `/api/v1/chats/{room_id}/messages?after_sequence=0&limit=50` | 按序读取消息与投递状态 |
| `POST` | `/api/v1/chats/{room_id}/messages` | Human 发 @消息或关联回复；请求含 UUID `idempotency_key`、`content` 和可选 `reply_to` |
| `GET` | `/api/v1/chats/{room_id}/turns` | 查看 Agent 回合的持久状态 |
| `GET` | `/api/v1/chats/{room_id}/events` | SSE 变更通知；连接后立即推送当前快照，断线可重连 |
| `POST` | `/api/v1/chats/{room_id}/turns/{turn_id}/cancel` | 取消本房间回合；未知或跨房间 ID 拒绝 |

发送新讨论必须提及至少一位 Agent，例如 `@白金 请讨论边界`；回复 Agent 消息时可省略
提及，也可额外提及队友。未知 @别名、只含提及、回复 Human 或跨房间回复都会拒绝。
同一创建键重试返回原房间；同一消息键重试返回原消息，改用相同键发送不同内容返回
`409`。消息回执中的 `discussion_queued` 和 `turns` 表示只读讨论的认领状态，
不是模型成功回答；`execution_authorized=false` 始终表示不授权编码任务。
兼容字段 `agent_dispatched=false` 只表示 HTTP 回执不确认模型已启动，不能用于判断
排队后的实际状态；请查询 `turns`。
创建与发消息不会创建编码 Task 或 Git Worktree。Agent 回复写回本聊天室，
`pending` 仍仅表示消息投递状态，**不是 Agent 已接话**。仅在可信
本机使用：当前没有多用户认证或公网部署保护。

SSE 只传消息序号和回合数量等失效提示，不传模型逐 Token 文本，也不是成功证据。
浏览器收到提示后重新读取 SQLite 支撑的消息与回合接口；若事件流断开，10 秒轮询
继续同步。真实 Agent 失败只在回合中显示安全类别或退出状态，不保存原始 CLI 错误。

测试见 `tests/test_standalone_chat_store.py` 与 `tests/test_standalone_chat_api.py`。当前没有对话运行时 Trace 事件或跨进程
派发；数据中的 `trace_id` 为下一阶段事件记录提供关联键，并不代表这两项已实现。
8.4a 时 60 项定向回归和全仓 Ruff 通过；全量离线检查仍有此前记录的 9 项 continuation
失败，另有 5 项 macOS Seatbelt 用例在本任务沙箱内受限，宿主授权环境单独重跑
10 项均通过。因此不宣称全量通过。8.4b 的定向验收见项目状态；8.4c 的内部无仓库
运行边界见[只读 Agent 文档](standalone-chat-agents.md)。8.4d 已接入有界消息调度，
失败、取消和重启残留回合不自动重试；独立聊天 UI 已由 P1 的 Fake 浏览器场景
验收，真实三模型浏览器验收仍属于 P2。
