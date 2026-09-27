# 第 3 步：HTTP 继续 API 与执行协调器

本步开放后端单目标、单回合继续执行，使用 Fake 完成离线验收。不是完整的继续→验证→
评审闭环，也没有开放 UI 人工输入或调用真实模型。可信本地部署限单进程、单 worker。

## 调用顺序

1. 正常任务流程请求人工澄清后，服务持久化 `needs_human`、Runtime 和等待 Trace。
   Planner 尚未产出 Plan 时也可以等待；不需要测试夹具或手动跑内部回合来暂停任务。
2. 使用已有 `POST /api/v1/tasks/{task_id}/messages` 提交补充消息或 `reply_to` 回复。
   消息 API 本身仍不派发 Agent；选择当前 Task 修订号、实际收到的问题及目标角色。
3. 调用下面的继续 API，立即获得 `202` 及持久化请求 ID，再查询执行状态。
4. 同一任务已有成功继续回合后，下一个新意图先调用已有 `/continuations/{request_id}/authorize`，
   再携带授权 ID 调用继续；授权命令中的键、消息、角色、Task 修订号必须完全匹配。
   失败、取消、隔离、未知 Claim 不解除占用，不能靠换键或新消息绕过。

## 执行契约

`POST /api/v1/tasks/{task_id}/continue`：

```json
{
  "expected_revision": 2,
  "message_id": "4bc1db7f-b0b5-4a96-b276-820a41904519",
  "target_role": "planner",
  "idempotency_key": "355fd38e-2c40-49c8-bdab-cb593a1e383f",
  "authorization_id": null
}
```

UUID/修订号仅为示例。`authorization_id` 可省略，但仅用于任务**首次**继续；后续必须提供
服务器记录的授权 UUID。禁止额外字段，不接受新 Prompt、PID、私有 token、任意角色、
预算重置或完成标志。整数严格校验；目标仅 Planner / Implementer / Reviewer。
授权继续的 `expected_revision` 取授权快照，而不是消费后的 Task 新修订号；重试保留原始
完整命令。同键改动授权 ID、消息、目标或修订号均为冲突，不会重新派发。

响应复用 `ContinuationStatus`：包含 `receipt.request.request_id`、`receipt.state`、
Task/Runtime 当前修订号、Task 状态和隔离信息，不返回 claim token。
`202` 表示请求已接受或历史请求已重放，不保证模型已启动、进程已停止或任务已成功。
`receipt.state=succeeded` 只证明该回合的输出、Runtime 和选定输入 ACK 已提交，
`task_completion_evaluated=false` 始终不变。

`GET /api/v1/tasks/{task_id}/continuations/{request_id}` 查询当前回执；它与长模型回合
不共享长锁。取消沿用 `POST .../continuations/{request_id}/cancel`（202）和
`GET .../cancellation`。取消命令的 Task/Runtime 修订号、Claim 时间戳取自最新查询，
只有实际持有回合的服务/事件循环能发起新取消；普通 Task cancel 拒绝本地活动继续回合。
本地活动回合期间也拒绝追加 Human 消息；结束并停回人工后再提交下一条新意图。

严格格式错误 422；任务/消息/授权不存在或跨任务作用域 404；修订、授权、预算、权限、
工作区或证据不匹配 409；账本不可用、损坏或未配置能力 503。错误不泄露密钥。

## 执行与事务边界

- 派发前重新检查 Task/Runtime、消息、Plan、Worktree、注册表、权限、Artifact Blob 和预算。
- 首次继续在同一事务内提交 fresh Claim、请求/认领 Trace 及 HTTP 命令哈希。
  后续继续复用第 2 步原子恢复，在同一消费事务中加入 HTTP admission Trace；没有独立的
  “授权已消费但 HTTP admission 未落盘”写入间隙。数据库保持 Migration 14，无新增表。
- 服务保留独立 asyncio Task 和私有本地 owner，锁仅覆盖准备/认领，不覆盖模型回合。
  重放校验完整 HTTP 命令哈希（包含授权 ID），只查询持久化结果，不接管内部 pending/
  staged Claim，也不重新出租未知 owner。认领后、派发前崩溃仍保留占用。
- 每次启动新会话，当前 Runtime 清空旧验证/完成状态及 native session；历史 Artifact 仅作上下文。
  只消费指定 Human 消息和对应 Handoff，不消费其他待办，不自动唤醒队友。
- 初次继续期间 Task 保持人工等待；授权继续按角色暂存 planning / implementing / verifying。
  回合提交后停回 `needs_human`；活动状态提交必须绑定已消费授权，不放宽普通状态转换。
  Reviewer 的 verifying 暂存状态不代表 Verifier 已运行；历史证据不能生成正式批准或返工结论。
- 授权回合成功时，Task 停放、Runtime CAS、ACK、回执和 Trace 同事务；失败或取消的停放
  与失败回执/Trace 同事务。提交 Trace 失败不留下假 ACK 或成功回执。
- 完成回调也覆盖协程尚未进入就被取消的情形；账本失败只留未知 Claim 并报告诊断类型，
  不重试模型。文件写入、已路由输出及实际用量属于跨事务副作用，不能回滚。
- 派发尝试与失败/取消费用沿用原账本；未知 Token 不填零，重放不收费，返工和预算不重置。

服务关闭取消本地回合；没有 Human cancel 请求时不伪造人工取消记录。
关闭时先停止新的继续认领；即使准备阶段正在等待 Git 检查，也不能在关闭后消费授权或派发。
历史查询/重放仍允许，重新 startup 才开放新认领。
`external_process_stopped_confirmed=false`，适配器终态不能证明 OS 全部派生进程或远端请求停止。
重启对未解决占用保持原围栏，不自动接管/补发/释放。服务级退出清理仍依赖适配器协作，
不是异常外部进程的硬性停止保证。

## 离线验收

`tests/test_continuation_execution.py` 新增 62 个用例，使用真实 Git / SQLite / Verifier
与 Fake Agent，以及 AsyncClient / ASGITransport 同事件循环 HTTP 请求，覆盖：

- 生产人工等待、Plan 前澄清、三个角色首次继续、纯 HTTP 首次继续及再次授权；
- 同键并发只运行一次、命令冲突、内部 Claim 拒绝接管、不同服务只查询不派发；
- 预算/权限/工作区/证据重新核验、严格契约、运行前预算变化和拒绝历史审批；
- 启动/退出/输出/提交失败、事务回滚、启动中/运行中/协程入口前取消、服务关闭；
- 损坏 admission 拒绝重放，关闭期间准备不认领，失败/取消不 ACK、不重跑、不生成完成判定。

context7-mcp 用于核对 FastAPI 响应模型与异步 HTTP 测试：后台回合采用显式持有的 asyncio
Task；测试 HTTP 与被取消回合共享事件循环，不用请求结束前等待的 BackgroundTasks。
333 项定向回归及关闭竞态专项通过；最终全量 `scripts/check_offline.py` pytest / Ruff
通过，九个在线开关关闭、模型凭据从测试子进程环境移除，没有调用真实模型。
macOS Seatbelt 用例需经授权在外层沙箱外运行；保留原有依赖弃用警告。

下一步固定为第 4 步：Fake 人工回复→继续→新验证→独立评审→完成守卫完整闭环，
覆盖失败、取消与重启。此步尚未实现的闭环及 UI 不计为已完成功能。
