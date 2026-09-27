# 第 5 项：受控人工消息 API

`POST /api/v1/tasks/{task_id}/messages` 接收补充要求或回复澄清，复用 SQLite
TeamRoom、角色路由、幂等持久化和 Human Trace。消息随后可从现有 GET messages 接口查看。
本项不派发 Agent、不执行测试、不运行完成守卫，不恢复任务，也不实现 UI 输入框。

## 前置条件与边界

- 当前任务已持久化为 `needs_human`，聊天室有效且未关闭；没有本地运行或取消操作。
- 使用 GET task 最新 `revision` 作为 `expected_revision`。消息只增加聊天室序号，
  不改变任务修订号、Issue、Plan、权限、返工轮数或预算；任务仍为 `needs_human`。
- 仅支持可信本地、单进程服务。发送者由服务端绑定该聊天室唯一 Human 成员；
  这不是用户登录鉴权或多用户身份系统，服务必须仅绑定回环地址，不应公开到公网。
- 不接受 sender、type、correlation、trace、任意收件成员/广播、Artifact 或审批字段。
  文本即使要求“跳过测试/重置预算”，也只是普通聊天，不变成执行授权。
- 不加载模型密钥或启动模型。不要在聊天中发送密钥，消息正文会持久化。

## 请求示例

下面是请求体示例；修订号须替换为任务最新值，幂等键由客户端生成 UUID。

给白金补充要求：

```json
{
  "expected_revision": 3,
  "idempotency_key": "dc716274-793b-43b7-ac47-cb9815783a7e",
  "recipient_role": "planner",
  "content": "请保持现有接口兼容，补充空输入的验收说明。"
}
```

允许收件角色：`planner`、`implementer`、`reviewer`、`orchestrator`。不支持 Verifier、
Human 或全房间广播；不解析 @昵称，不能用昵称绕过角色路由。

回复实际收到的澄清或转人工请求（不要同时提供 `recipient_role`）：

```json
{
  "expected_revision": 3,
  "idempotency_key": "1e971b22-a2aa-4b92-853a-2bd8771d5fda",
  "reply_to": "7323391b-0473-41ae-9fda-3891a61ae824",
  "content": "我已确认边界，请保留失败证据，暂时不要追加返工轮次。"
}
```

示例消息 ID 仅说明格式；必须使用当前 API 任务自己的消息 ID，不能向历史 smoke 归档
写入或拿其他任务的消息来回复。服务端从父消息推导收件成员、correlation_id 和
causation_id，不接受客户端伪造。仅允许回复发给此 Human 且仍待处理的 `question`
或 `human_input_request`；前者生成 `answer`，后者生成普通 `message`。
其他任务或缺失消息返回 404；未投递、已 ACK、非提问/人工请求的消息返回 422。

## 回执、幂等与错误

成功（含合法重复提交）返回 HTTP 201：

```json
{
  "message": {"sequence": 12, "message_id": "...", "sender_role": "human", "content": "..."},
  "task_revision": 3,
  "agent_dispatched": false
}
```

这里只展示关键字段；完整响应复用 `RoomMessageView`，包含发送者、收件人、关联 ID
及时间。HTTP 201 只代表消息入库，绝不代表 Agent 已执行或任务完成。

- 相同任务、Human、幂等键和相同语义内容只产生一条消息和一条消息 Trace；
  并发重试及服务重建仍返回原 message_id、sequence、created_at。
- 同一个键改变正文、目标或回复对象返回 409 `task_message_conflict`。新意图使用新键。
- 修订号不符、未暂停、仍在运行/取消、聊天室关闭：409 `task_state_conflict`。
- 身份缺失/歧义或聊天室上下文不匹配：409 `task_detail_unavailable`。
- 内容为空/超过 16000 字符、伪造字段、目标组合不合法：422；未配置服务：503。
- 每次重试仍检查当前状态、修订号和回复待处理状态，不在任务已恢复/结束后补写消息。

本步不 ACK 原人工请求或新消息；回复不会自动标记该请求已处理。没有新增数据库迁移，
幂等使用现有聊天室唯一约束与内容指纹；消息和 Trace 沿用现有独立事务。若写 Trace
失败，消息可能已入库，合法重试可补齐 Trace，不能将此接口宣称为跨事务原子提交。
单 worker 锁不提供多进程协调能力。

## 验证与下一步

离线测试使用真实 API/SQLite/router 与临时 Git 仓库，工作流为受控模拟，不调用模型：

```bash
.venv/bin/pytest -q tests/test_human_message_api.py tests/test_task_api_contract.py tests/test_persistent_task_service.py
.venv/bin/python scripts/check_offline.py
```

第 6 项第 1 子步骤已实现[只读继续预检](continuation-preflight.md)，尚不消费消息或派发。
后续实现显式继续/唤醒、幂等消费和预算边界；第 7 项再接 UI 聊天输入框。
当前状态模型仍将 `needs_human` 视为终止等待态，本项没有放开它的出边转换。
