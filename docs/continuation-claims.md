# 第 6 项第 3 子步骤：持久化继续请求与幂等认领

本步将第 2 子步骤的单回合内核接到 SQLite 认领账本。仍然没有 HTTP 执行接口、UI
继续按钮或自动状态回归；测试只使用 Fake Agent 和临时仓库，不调用真实模型。

后续第 4 子步骤 A 已接入[失败与取消尝试记账](attempt-accounting.md)，
替代本页开发时的失败成本待实现状态；人工解除占用和状态回归仍未完成。

## 请求与回执

内部 `HumanContinuationKernel.run_single(task_id, request, idempotency_key=UUID(...))`
接受明确的幂等 UUID。省略时为内部调用生成该任务/Human 消息对应的确定性 UUID；未来
HTTP 契约必须要求显式键。任务、已有消息、目标角色与 expected_revision 是客户端
命令身份；服务端补充 trace/room、目标成员、绑定名称、Runtime revision 和原消息摘要哈希。
正文仍留在聊天室，账本不复制聊天历史，不允许角色或预算重置字段。

同一任务/幂等键只能对应同一命令；改消息、角色或 expected_revision 会冲突。同一
Human 消息也只能建立一个继续请求，不能换键重跑。Partial Unique Index 使同一任务
最多有一个 Pending、Claimed 或 NeedsHuman 记录；失败/不确定记录不能靠新消息绕过。
完成本次单回合后可用新 Human 消息申请下一次回合，原累计预算仍保留。

回执 `scope=single-agent-continuation`，含请求身份、状态、成功提交的会话 ID、输出/
消费消息 ID、Runtime revision，或安全失败码。`task_state_at_request=needs_human`
仅描述申请时状态，`task_completion_evaluated=false`；请求的 `succeeded` 不等于
任务完成。当前内核不修改 Task，也不运行完成守卫。

重复调用返回原持久化回执，`replayed=true`，不重建 Runtime、不运行模型、测试或
工作流，也不重复写 Trace/ACK/预算。重放时 `prepared/result` 为 None，不能把它
当作新执行结果。成功请求即使输入已 ACK，也可重放；不再返回上一子步骤的 ACK 冲突。

## 状态与原子边界

```text
prepare → pending → claimed → succeeded
                         └→ needs_human
```

- `pending`：已持久化请求，尚未认领。相同显式调用仍须重新准备并核验原 Runtime
  revision；不能自动改基线、角色、预算或消息。如果条件已变化，保留占用并拒绝执行。
- `claimed`：SQLite `BEGIN IMMEDIATE` 内重新校验 Task/Runtime revision、房间/角色
  绑定、Human 正文哈希及 Pending 投递，再执行 state/token CAS。只有一个调用者
  获得私有 claim_token；其他调用者只能读取，不把 token 暴露到回执或 Trace。
- `succeeded`：单回合契约校验、输出路由及账本记录成功，最终提交也成功。
- `needs_human`：异常、取消或回合前预算阻止导致本请求停止；没有自动重试或解除占用。

认领取得前不发布 Handoff、不启动 Agent。指定输入的 ACK 延迟到最终事务；同一事务
提交 Runtime CAS、原 Human 消息和 Handoff 的选定 ACK、成功回执以及成功 Trace。
任一检查/写入失败即整体回滚，不能出现“回执成功但 Runtime/ACK 未提交”。只有持有
当前 claim_token 的调用者能完成或暂停请求，终态不能重新认领。
申请、认领、成功及暂停四类 Trace 也与各自状态事务同生共死，保留原 trace_id 和
correlation/causation。TraceStore 新增调用方事务入口，拒绝非事务连接。

## 重启与故障语义

这是请求路径的持久化 **at-most-once 派发**，不是外部 CLI 的 exactly-once 保证：

- 曾认领的请求永不因时间流逝或重启自动重新租用。重启后读取 `claimed` 只证明
  执行权已占用，不能证明旧 Agent 仍在运行，也不能证明它没运行过。
- 同键请求返回这个不确定回执，不再次启动 Agent。需要先人工检查旧进程、文件与
  证据；本步没有实现解除占用/重试授权。服务启动仍不自动执行 `needs_human`。
- 执行中异常/取消尝试记录 `needs_human`。若数据库不可写，保留旧 `claimed`，
  不吞掉原异常，也不把占用释放后重跑。
- Agent 文件修改、输出路由和用量账本仍在最终事务之外。提交失败不能撤回这些
  副作用；输入 ACK 与 Runtime 会回滚，已认领记录阻止把同一意图再派发一次。
- 成功回执只是持久化引用索引，重放不重新审计输出内容或证明当前代码仍通过测试。

仍只支持可信本地、单 worker 产品部署。独立 SQLite 连接可争抢认领，不等于完整多
worker 调度/生命周期安全；本地服务锁仍覆盖回合，尚未接后台执行和 HTTP 取消入口。
本步不增加正式评审能力：历史证据不能直接 approve_review/request_rework，仍须后续
重新验证和受控状态回归。

## 测试与下一步

```bash
.venv/bin/pytest -q tests/test_continuation_claims.py tests/test_continuation_runtime.py tests/test_agent_turn_runner.py tests/test_workflow_execution.py tests/test_continuation_preflight.py tests/test_trace_store.py tests/test_human_message_api.py tests/test_persistent_task_service.py
.venv/bin/python scripts/check_offline.py
```

覆盖注册/回执持久化、同键异意图、换键重复、任务占用、交易内作用域与修订检查、原
Human 正文变更、6 个独立连接争抢、错误 owner/终态拒绝、Pending 的显式继续、独立
服务锁在异步准备期间的相同请求竞争与回执重放、独立
子进程遗留 Claim 不重跑、最终 Trace 故障回滚所有 ACK/Runtime、CAS 冲突、损坏记录
失败关闭以及严格字段/回执证据约束。既有单目标、预算、取消、权限和消息 API 回归保留。

下一步是第 4 子步骤：预算与失败恢复收口，包括失败/取消的成本记账、不确定 Claim 的
人工处置授权、运行中取消和受控状态回归。完成 Fake 闭环后再接 HTTP 执行与第 7 项 UI。
