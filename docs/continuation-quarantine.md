# 第 6 项第 4 子步骤 B：不确定 Claim 的人工隔离

后续第 4 子步骤 C 已接入[本地服务级取消](continuation-cancellation.md)。本步隔离
仍不取消进程或释放占用；只有本服务实际持有的继续回合才能请求新的 API 取消。

本步提供可审计的**隔离处置**，不是“解除占用并重跑”。系统目前没有足够的
外部 CLI 停止证据，所以没有重试/释放接口，也不接受 Human 传入一个布尔值
来冒充确定性停止证明。所有验证均为离线 Fake/SQLite/HTTP 测试。

## API 契约

只支持可信本地、单 worker 产品部署；房间 Human 是服务端绑定的本地身份，
不是远程用户认证。不要把隔离原因当作密钥配置入口。

`GET /api/v1/tasks/{task_id}/continuations/{request_id}` 返回：

- 原继续请求回执及其 `updated_at`，不暴露私有 `claim_token`；
- 一致读取快照中的当前 Task revision、Runtime revision 和 Task state；
- 已有隔离回执（没有则为 `null`）。GET 不写 Trace、不执行模型或工作流。

request ID 可从原 `continuation_requested/claimed/paused` Trace 的 `request_id`
获取。尚未开放 HTTP 继续执行入口，此查询主要供恢复、诊断和后续 UI 使用。

`POST /api/v1/tasks/{task_id}/continuations/{request_id}/quarantine` 示例：

```json
{
  "idempotency_key": "f7e5ca36-c258-4e7e-a7c8-014e31c0d8a2",
  "expected_revision": 3,
  "expected_runtime_revision": 2,
  "expected_claim_updated_at": "2026-09-28T01:00:00Z",
  "disposition": "quarantine",
  "reason": "旧执行结果不确定，保留占用，待核验进程和文件副作用。"
}
```

UUID 是示例；三个快照字段必须取自实际 GET，不能照抄。修订号严格整数且至少 1，
原因去除首尾空白后长度为 1～1000；拒绝未知字段，以及任何预算重置、释放、重试、
Human 身份、权限升级或成功声明字段。

新隔离要求：任务仍为 `needs_human`、房间有效且唯一 Human 身份匹配，Task/Runtime
revision 和 Claim 时间戳未变化，原请求处于 `claimed` 或 `needs_human`。
`pending` 和已 `succeeded` 请求不可隔离。不存在/跨任务对象为 404，过期/不合法
状态或幂等冲突为 409，请求格式为 422，损坏账本等不可用情况为 503。

同任务/同键/同命令返回完全相同的隔离回执，即使当前修订号后来前进，也不把历史
决定伪装成新的处置。换原因/请求/修订号复用键拒绝；对同一请求换键也不能覆盖决定。
没有本步提供的解除隔离或重新授权能力。

## 存储与提交围栏

Migration 11 增量创建 `continuation_quarantines`，不改 Migration 10 的校验和及原 Claim。
每个继续请求最多一条不可覆盖的隔离记录，保存 Human 命令、身份、原记录 SHA-256
和范围；JSON 与索引字段、原 Claim 快照均校验一致。没有复制原聊天正文或 Agent 历史。

`BEGIN IMMEDIATE` 内一起提交隔离记录和 `continuation_quarantined` Human Trace：
保留原 trace/correlation/causation。Trace 写入失败则隔离一起回滚，不能给调用者一个
实际上没有持久化的隔离成功回执。

原执行者的 `finish/pause` 必须检查隔离围栏；即使持有正确 token，隔离先提交后也
拒绝其 Runtime/选定 ACK/成功回执或暂停回执更新。如果成功提交先获事务执行权，
隔离会发现状态/时间戳变化并拒绝，不改写既有成功结果。此成功仍只表示单回合提交，
不是完成守卫通过或任务完成。

## 保留边界

- Task 和原 Claim 保持原样，任务占用仍保留。原意图或新 Human 消息都不能绕过占用。
- 不重置预算/返工次数，不 ACK 原消息，不修改 Worktree、Runtime、Plan、验证证据。
- 回执固定 `claim_released=false`、`external_process_stopped_confirmed=false`、
  `agent_dispatched=false`、`budget_reset=false`、`task_completion_evaluated=false`。
- **不是 OS 隔离或进程取消。**旧 CLI 仍可能运行或修改文件；在围栏之前发生的 Agent
  文件写入、输出路由和预算记账不会回滚。隔离不表示 Patch 已经安全。
- 当前本地服务锁仍覆盖单回合；同实例 POST 可能等待该锁，不能当作运行中即时取消。
  外部独立连接提交围栏的交错语义已离线验证，不等于完整多 worker 生命周期安全。
- 成功继续回执的重放仍返回原回执；隔离状态须通过新查询接口读取，不作为执行许可。

## 验证与下一步

`tests/test_continuation_quarantine.py` 覆盖作用域/身份/修订号、固定 False 契约、幂等、
GET 无副作用与隐藏 token、旧数据库增量迁移、Trace 回滚、独立 SQLite 连接争抢、
Agent 输出后围栏拒绝迟到提交、提交先获胜不能被重标记、损坏记录失败关闭。

下一步对接服务管理的取消与停止证据；随后设计显式新意图的重新授权与受控状态
回归。现阶段不释放未知进程的 Claim，不开启 UI 继续按钮，不放宽 CompletionGuard。
