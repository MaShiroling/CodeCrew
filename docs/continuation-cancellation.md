# 第 6 项第 4 子步骤 C：服务级取消与停止观察证据

本步管理**本服务、本事件循环实际持有**的单目标继续回合。没有 HTTP 继续执行入口、
UI 继续按钮或解除 Claim 的能力。测试仅用离线 Fake、SQLite、ASGI 和已有本地进程测试。

## 取消契约

`POST /api/v1/tasks/{task_id}/continuations/{request_id}/cancel` 返回 `202`。
请求格式：

```json
{
  "idempotency_key": "4bc1db7f-b0b5-4a96-b276-820a41904519",
  "expected_revision": 3,
  "expected_runtime_revision": 2,
  "expected_claim_updated_at": "2026-09-28T01:00:00Z",
  "reason": "停止当前本地继续回合，保留文件与证据供人工检查。"
}
```

UUID 是示例；修订号和时间戳取自已有 Claim 查询，不能照抄。严格整数、非空原因
和禁止额外字段的规则与隔离接口一致，不允许传入 PID、Human 身份、释放/重试或成功标志。
服务端绑定唯一 Human 身份，重新校验 Task/Runtime/Claim、房间和本地 owner。

取消入口不等待长回合锁：先将取消意图及 Human Trace 原子持久化，再对实际持有的
asyncio Task 请求取消。`202` 仅证明**意图已接受**，不表示 OS 进程已停止。进程在
写入后、发送信号前崩溃，记录可能仍为 `requested`；重启不自动补发或猜测 PID。

同键同命令重放当前持久化回执，不再次调用取消；不同键/原因不能覆盖已有决定。
重放已有取消不需要本地回合仍存在。没有本地 owner 的未知 Claim 不接受新取消请求
（409），应走[人工隔离](continuation-quarantine.md)，不能杀任意外部进程。
已成功或尚未认领的请求也不支持新取消。产品仍限可信本地、单 worker、同一事件循环。

## 查询与证据

`GET /api/v1/tasks/{task_id}/continuations/{request_id}/cancellation` 读取取消回执；
没有记录为 404，不写 Trace、不派发。回执状态：

- `requested`：意图已持久化，尚无成功持久化的观察；停止情况未知。
- `observed`：已持久化观察结果，**不等于确认所有进程停止**。

观察分为 `adapter_terminal_result`、`no_session`、`cleanup_failed`、`cleanup_timed_out`、
`invalid_result`、`no_observation`。收到匹配会话/trace/角色/适配器的终态时，将原
`AgentResult` 保存为 Artifact，回执只携带引用、会话 ID、退出原因及退出码；不从
聊天自述推断停止。不匹配的结果不作为证据或 Token 用量。

原始适配器 `cancel()` 返回不足以确认停止；运行器再限时等待终态结果。默认清理
期限 5 秒，仅为内部设置，不接受客户端覆盖；asyncio 超时依赖适配器协作响应取消。
超时/错误保留未知，不能保证异常适配器或继承句柄的子进程已退出。Fake 终态没有
真实 OS 进程；CLI 的终态报告也不证明全部派生进程、远端请求或文件副作用已停止。

回执始终 `external_process_stopped_confirmed=false`、`claim_released=false`、
`budget_reset=false`、`task_completion_evaluated=false`。不要将 `exit_reason=cancelled`
外推成完整 OS 进程树证明。更强的停止证据及新意图授权仍待后续实现。

## 持久化与围栏

Migration 12 新增 `continuation_cancellations`，私有 owner token 不进入回执、Trace
或 Artifact。请求和观察分别与对应 Trace 事务内提交；失败回滚，不产生假停止回执。
索引/JSON/原 Claim 范围及 owner 绑定均校验，损坏数据返回不可用，不重新派发。

持久化取消阻止单回合最终成功提交，即使适配器吞掉协程取消；正常处理将 Claim
保留为 `needs_human/cancelled`。没有完成守卫、ACK、Runtime 或 Task 状态回归。
取消期间已发生的文件修改、输出路由和用量不可回滚；需要人工检查。若账本写入
失败，原 `claimed` 或 `requested` 记录仍保守保留，重放不补发模型/取消。

服务关闭会先请求取消本地继续回合，再取得长工作流锁，避免等待阻塞回合才能取消。
直接取消协程或服务关闭（非 Human API 请求）仍沿用原暂停/诊断，不虚构 Human 取消意图。
清理期间同键请求不再次 `Task.cancel()`，避免打断第一次证据收集。

## 预算及验收

取消回合仍占用实际预留的尝试；匹配终态提供的原生 Token 可以补记，未知保持 NULL。
尚未进入派发尝试的回合不凭空增加模型尝试次数。不重置任何历史预算/返工轮数。

`tests/test_continuation_cancellation.py` 覆盖锁内运行的 sideband 取消、启动中/运行中/
运行器入口前取消、清理失败/超时、会话错配、幂等、未知 Claim 拒绝、严格作用域、
Trace 回滚、损坏账本、吞掉取消的适配器、服务关闭与原生 Token 记账。HTTP 测试用
AsyncClient/ASGITransport 与被取消回合共享事件循环，不访问网络或真实模型。

下一步是显式新意图的重新授权与受控状态回归；尚未允许自动释放未知进程 Claim，
也未实现完整多 worker 取消、OS 进程树停止认证或 UI 驱动真实任务验收。
