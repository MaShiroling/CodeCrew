# 显式新意图重新授权（第一部分第 1 步）

本步交付授权**记录**与查询，不交付执行接口、任务状态回归或旧 Claim 解锁。
适用场景：一次内部单目标继续回合已经 `succeeded`（Runtime/ACK/回执已提交），
任务仍为 `needs_human`，用户提交另一条人工消息，明确选择后续目标并留下授权原因。
这里的 `succeeded` 只描述一个已提交回合，不描述任务成功，也不是 OS 进程树停止认证。

## API

`POST /api/v1/tasks/{task_id}/continuations/{previous_request_id}/authorize` 返回 200。
请求示例：

```json
{
  "idempotency_key": "7b819725-ecc4-4f7b-a3c9-336ed9c44202",
  "message_id": "2c032f76-8e89-4b5f-8a0b-803b17823a60",
  "target_role": "planner",
  "expected_revision": 3,
  "expected_runtime_revision": 4,
  "expected_claim_updated_at": "2026-09-28T03:00:00Z",
  "reason": "请先按新增兼容性要求重新检查方案。"
}
```

UUID、修订号和时间戳均为示例；旧请求时间戳来自 Claim 查询，Runtime 修订号使用当前值。
`message_id` 必须是已有 Human API 产生的**新**消息或关联回复，尚未 ACK，且只投递
给选定 Agent 或 Orchestrator。客户端不能指定 Human 身份、PID、释放占用、重置预算
或任务成功。严格整数、带时区时间戳、非空原因及额外字段拒绝在请求边界校验。

`GET /api/v1/tasks/{task_id}/continuation-authorizations/{authorization_id}` 只读查询。
不存在或跨任务访问返回 404；冲突返回 409；损坏账本/SQLite 故障返回 503。
这是可信本地单进程接口，不是远程认证或多租户权限系统。

## 允许与拒绝

新授权复用只读准备：任务/房间状态、目标注册表与权限、Worktree、验证计划、绑定、
Artifact 完整性、返工和对话预算均重新检查；预留后续 Handoff 所需的消息预算空间。
事务提交前再次验证 Human 消息、Task/Runtime 修订号与 Runtime 内容哈希。
上一回合的提交修订号必须等于当前 Runtime 修订号，不能越过中间回合拿旧成功授权。

以下情形拒绝，不修改旧请求：

- 前一请求 `pending`、`claimed`、失败、取消或隔离，或任务还有任何未解决继续请求。
- 取消回执是 `observed`，甚至附有匹配的适配器终态，但旧请求没有成功提交。
- 复用旧消息、已消费的消息/执行键，或选择与收件人不匹配的角色。
- 预算耗尽、配置/工作区/证据失效、修订号或旧 Claim 时间戳变化。

取消观察与人工隔离都不能充当释放许可。状态未知的执行仍需更强的停止证据和单独的
恢复策略；本步未实现这类故障重试，不能声称失败/取消任务已可恢复。

## 审计、幂等与边界

Migration 13 增加 `continuation_authorizations`。记录绑定旧请求哈希、新人工消息哈希、
目标 Agent、当前 Runtime 哈希及 Artifact 引用；不写入私有 claim token。
Human `continuation_authorized` Trace 与回执同事务提交，失败则全部回滚。
每个任务的同一消息、同一 Runtime 修订仅允许一条授权决定；独立连接争抢由唯一约束
仲裁。新增授权不创建 `pending` 执行请求，也不改变旧 Claim 或消费任何消息。

同键同命令返回原始回执，不重新预检、延长授权或追加 Trace；更换目标/原因等会冲突。
查询与重放返回的是历史快照，即便当前 Runtime、预算或配置已变化也不能直接执行。
新消息绑定或旧请求记录损坏时拒绝读取/重放，不掩盖为正常历史数据。

回执始终：`execution_ready=false`、`agent_dispatched=false`、`claim_released=false`、
`budget_reset=false`、`task_completion_evaluated=false`、
`external_process_stopped_confirmed=false`。审批/测试证据只是历史上下文，不复用完成结论。

当前可信内部 `HumanContinuationKernel.run_single` 接口保持原状，不以本记录作为可执行
许可证；没有新 HTTP 执行接口或 UI 按钮。后续受控恢复/执行入口必须重新核验本记录、
消息、修订号、预算、工作区及证据，并以持久化认领防重复派发。
撤销/替换授权、故障 Claim 解锁、任务状态回归与真实模型验收均不属于本步。

## 离线验收

`tests/test_continuation_authorization.py` 使用 Fake Agent、真实 SQLite/Git/Verifier 与
同事件循环的 ASGI HTTP 测试。覆盖三个角色、持久化重开、幂等与历史重放、未解决 Claim、
匹配取消终态不能解锁、严格字段、预算和证据/权限故障、Trace 回滚、记录损坏、
消息篡改、独立连接竞争和跨任务查询；不调用真实模型。

下一步：受控状态恢复，并定义授权快照在实际执行前的再次核验与消费规则。
