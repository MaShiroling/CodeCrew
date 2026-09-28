# 第 5 步：聊天室人工消息与关联回复

任务详情的“团队对话”页在 Task 为 `needs_human` 时显示人工消息表单。用户可以选择
Planner、Implementer、Reviewer 或 Orchestrator 发送补充信息；对仍投递给 Human 的
`question` / `human_input_request` 可点击“回复这条消息”，表单改为只发送 `reply_to`，
收件人、`correlation_id` 和 Human 身份仍由服务端决定。其他任务状态不显示表单。
“进行中”筛选包含待人工任务，避免把可处理的任务误归为已结束。

页面调用现有 `POST /api/v1/tasks/{task_id}/messages`，提交当前 Task 修订号、消息正文、
幂等键，以及 `recipient_role` 或 `reply_to` 二选一。前端限制空白/长度、禁用请求期间的
重复提交，成功回执才清空草稿；网络结果不明时保留内容和同一意图的幂等键，不自动重发。
`409` 尝试刷新任务/对话，仍要求用户确认后手动操作。晚到响应不会改写另一个任务的详情。
消息内容使用 DOM `textContent` 展示，不解释为 HTML。

消息列表新增只读 `pending_for_human` 字段，由服务端真实投递状态计算；UI 仅对尚待 Human
处理的问题显示回复入口。此字段只辅助展示，服务端仍检查任务状态、目标消息、投递状态、
修订号和角色权限。成功发送仅表示消息已持久化，**不会启动 Agent、恢复 Task 或判成功**。
继续/取消受控回合、预算和阻塞原因展示现已在[第 6 步](ui-controlled-workflow.md)接入，
但仍必须由用户单独明确操作。

离线验证包括 `tests/ui_human.test.cjs` 的表单/关联回复、发送限制、幂等重试、冲突和
切换任务竞态，以及 `tests/test_human_message_api.py` 的真实 SQLite 投递状态测试。
上述定向测试通过；全量离线套件有 9 项 continuation 既存失败，在本步修改前的
干净 `HEAD` 快照亦复现，不能记为全量通过。
仍未进行第 8 步要求的完整 UI 端到端真实模型验收；页面仅适合可信本机，不应公网暴露。
