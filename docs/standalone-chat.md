# 独立聊天室数据契约（8.4a）

本增量仅提供领域模型和 SQLite 存储，不开放 HTTP、Agent 调度或 UI。创建聊天室
不会调用 `PersistentTaskService.create_task`，不需要用户 Git 路径，也不会创建 Worktree、
调用 Verifier 或进入 CompletionGuard。后续功能必须显式接入，不能把旧任务聊天室
视作独立入口。

## 持久化边界

- `StandaloneChatRoom` 有独立 `room_id`、`trace_id`、标题、开关状态和四位成员：
  Human、Planner、Implementer、Reviewer。它没有 `task_id` 或 `repository_path`。
- `StandaloneChatMessage` 记录发送者、1～3 位具体收件成员、文本、关联回复、
  `correlation_id`、`causation_id` 和幂等键；不承载计划、代码、审批或完成动作。
- Migration 15 新建 `standalone_chat_*` 四张表，保留既有任务表及其迁移不变。
  同一发送者在同一房间复用幂等键时，只允许语义相同的重放。
- 投递状态按收件人独立保存；ACK 幂等。回复和因果引用必须留在同一房间及
  同一 `correlation_id`，关闭后不接受新消息，但允许读取与相同请求重放。

测试见 `tests/test_standalone_chat_store.py`。当前没有对话运行时 Trace 事件或跨进程
派发；数据中的 `trace_id` 为下一阶段事件记录提供关联键，并不代表这两项已实现。
60 项定向回归和全仓 Ruff 通过；全量离线检查仍有此前记录的 9 项 continuation
失败，另有 5 项 macOS Seatbelt 用例在本任务沙箱内受限，宿主授权环境单独重跑
10 项均通过。因此不宣称全量通过。下一步 8.4b 将独立存储接入聊天室 API，验证
创建和发消息都不触发编码任务。
