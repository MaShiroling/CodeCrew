# 第 2 步：受控状态恢复与授权消费

当前更新：第 3 步已通过[HTTP 执行协调器](continuation-execution.md)接线；本页描述
恢复内核本身的无派发契约，历史消费回执仍不表示模型已启动。完整继续闭环待第 4 步。

本步提供内部 `ControlledResumptionKernel.resume(task_id, authorization_id)`，不新增 HTTP
执行路由，不启动 CLI，不自动推进聊天室工作流。它把第 1 步的授权快照重新核验并消费，
原子恢复 Task/Runtime 和创建一次新的 Claim，供第 3 步执行协调器接线。

## 状态映射

| 目标 | 恢复状态 | 约束 |
| --- | --- | --- |
| 白金 / Planner | `planning` | 重新处理指定 Human 意图，不自动派发队友 |
| 月见 / Implementer | `implementing` | 旧验证失去当前证据资格，后续必须重新验证 |
| 鲸鲸 / Reviewer | `verifying` | 必须先进行新一轮确定性验证，不直接恢复 `reviewing` |

普通 `Task.transition_to` 仍禁止离开终态；仅可信恢复存储调用受限的
`resume_for_continuation`。它只接受 `needs_human` → 上表三种状态，不能跳到
`reviewing/completed/rework`，不能复活已完成、失败或取消的 Task。

## 再次核验与原子提交

1. 持有本地服务锁，读取任务范围内的授权和消费账本。已有消费只重放历史回执。
2. 重新检查消息、Task/Runtime 修订号、工作区、配置、注册表权限、预算和 Artifact Blob。
   当前目标、收件人、correlation、消息哈希、Plan 和证据引用必须与授权一致。
3. 在 SQLite 事务内重新核对授权/旧成功回合绑定、Runtime 内容哈希、最新 Plan、Artifact
   元数据、唯一 Human 身份及无其他未解决继续占用；再次检查累计预算和返工限制。
4. 同事务提交授权消费、Task 修订 +1、Runtime 修订 +1、新 `claimed` 请求，以及请求、
   认领、恢复 Trace。任一失败全部回滚，不能留下部分恢复或已消费但无占用的记录。

Migration 14 增加 `continuation_resumptions`，不改既有迁移。授权 ID 唯一、新请求 ID
唯一，再加原有任务占用索引防并发消费。新请求沿用授权选定的消息/角色/执行键，只将
Task/Runtime 预期修订推进到恢复后的值；旧请求与原授权记录不修改。

返回的恢复回执不含私有 claim token；首次成功的**可信内部返回值**可携带 owner 和
准备好的 Runtime。重放、独立连接输家和重启后查询都没有 owner、Runtime 或输入对象，
不能凭历史回执再次执行。损坏账本、索引/JSON/授权/Claim 绑定不一致时拒绝重放。

## 历史证据与预算

恢复后的所有 native session ID 清空；运行上下文不加载旧 `latest_verification` 或
`latest_completion`。旧 Plan/Diff/日志引用仅用于历史上下文，原 Blob 和 Trace 不删除。
“失去当前证据资格”不是删除历史证据，也不是证明代码已经安全。

恢复不 ACK Human 消息、不创建 Handoff、不路由模型输出、不运行 Verifier/Reviewer/
CompletionGuard。返工次数、累计尝试、Token 和时延保留原值；没有实际派发时不凭空计
一次模型尝试。Task 原需求、仓库、元数据和 Worktree/验证策略不变。

回执固定 `authorization_consumed=true`、`historical_evidence_invalidated=true`；
`agent_dispatch_ready=false`、`agent_dispatched=false`、`claim_released=false`、
`budget_reset=false`、`task_completion_evaluated=false`。当前状态是恢复已暂存，并非
Agent 正在运行，更不是任务完成。旧单目标内核不支持提交这种活动状态 Claim；
实际执行和失败/取消后的停放由第 3 步专用协调器处理，不能直接套用旧接口。

## 重启与失败边界

通用启动恢复器在扫描及实际恢复入口都检查继续占用：发现未解决 Claim，不自动
消费聊天室旧事件，不恢复旧完成证据，将活动任务保守停回 `needs_human` 并留 Trace。
Claim 和授权消费保留，既不重租，也不重置预算；损坏占用不能靠索引伪装成功绕过围栏。

事务提交后、真实派发前崩溃，授权仍已消费；本步没有自动撤销或重新出租机制。
若外部 SQLite 故障导致提交结果未知，也必须先查账本，而不是重新派发。
失败、取消、隔离或未知的旧 Claim 仍不释放；没有完整 OS 进程树停止认证。
内部恢复调用还不能当作用户可用的“继续执行”功能。

## 验收与下一步

`tests/test_controlled_resumption.py` 使用 Fake 与真实 SQLite/Git/Verifier 历史夹具，
覆盖三个角色、状态约束、预算/权限/证据重新核验、准备期间变化、事务 Trace 回滚、
独立连接竞争、幂等、损坏记录、重启停放和扫描后再次检查占用；不调用真实模型。

下一步为第 3 步：HTTP 继续 API 与执行协调器。接线必须在首次消费返回的 owner 上
启动一次受控执行，跟踪运行/取消，正确提交或停放；重复和重启请求只查回执。
同时需要补齐首次人工等待的授权/执行入口：现有第 1 步授权要求有前一成功继续回合，
不能把临时手动跑内部回合作为产品前置条件。完成后再在第 4 步验收完整 Fake 闭环。
