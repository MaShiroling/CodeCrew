# 第 6 项第 2 子步骤：Runtime 恢复与单目标回合内核

本步提供内部 `HumanContinuationKernel`，尚未提供 HTTP 执行接口或 UI 按钮。
目标是证明人工意图能恢复必要上下文，并只交给一个 Agent；不是让整条工作流重新运行。
离线测试使用 Fake Agent、真实临时 Git Worktree、SQLite 和 Verifier，不消耗模型额度。

## 两个内部入口

- `prepare(task_id, request)`：在服务锁下重新预检，读取持久化 Runtime、核验管理中的
  Worktree 与任务/上下文一致，检查当前验证计划、全部角色绑定及目标注册表的权限/能力。
  Git 检查有异步等待，因此返回前再次检查 task/runtime revision、消息投递和预算。
  恢复最新验证/评审/完成证据，检查 Plan、Diff、验证报告、权限报告、命令审计及日志的
  task/trace/type/hash；引用超限、缺失或损坏时失败。此入口不写消息、Trace、ACK 或状态。
- `run_single(task_id, request)`：重新准备，不接收或信任之前返回的准备对象；发布一个
  确定性 Orchestrator Handoff，携带原人工正文、correlation/causation 和证据引用。
  明确选择输入 ID，启动一次新会话，记录输出/Trace 和现有预算账本，保存 Runtime。
  输出消息只留在聊天室，不调用事件循环、Verifier、其他 Agent 或 CompletionGuard。

Human 直发目标时，仅消费该 Human 消息与本次 Handoff；发给 Orchestrator 时，目标
仅消费转交 Handoff，成功后才 ACK 原人工消息。其他历史待办、其他人工意图不被消费。
所有目标都使用新会话，尤其 Reviewer 不恢复任何旧 native_session_id；权限沿用
Planner/Reviewer 只读、Implementer Worktree 写入。注册表排队结束后再次检查选定投递
仍 Pending，避免排队期间已 ACK 的输入启动重复回合。默认普通回合仍消费原有待办批次。
这里检查权限接线，不将 Fake 运行当作真实 CLI 的 OS 沙箱证明。

## 状态与证据边界

本子步骤没有开放 `needs_human → planning/implementing/reviewing` 通用转换。
一次受控对话后任务仍为 `needs_human`，Task revision、返工次数不变；仅保存新的 Runtime
revision。启动恢复仍不自动派发人工等待任务。未来状态回归必须由持久化继续请求授权，
而不是由消息正文或 Agent 自述决定。

恢复出来的证据是历史证据，不能证明当前 Worktree 仍与验证时一致：

- 所有目标的 `runtime.latest_completion` 清空，历史决策仅保留用于诊断。
- Implementer 的 `runtime.latest_verification` 清空；旧报告仍可通过引用读取。
- Implementer/Reviewer 必须有当前 Plan；Reviewer 交流还必须有验证与 Diff 证据。
- 本路径在路由/ACK 前拒绝 `approve_review` / `request_rework`，避免用历史验证生成
  正式评审结论；原始输出和接收到的流事件仍作为诊断记录保存。
- Planner 可发布新版本 Plan，Implementer 可提出澄清/请求评审，但后续事件暂不自动处理。
  正式验证、评审和完成必须走后续受控工作流，不能直接消费这些结果宣布成功。

继续沿用实际 Guard、当前累计用量和返工上限；不给预算归零，也不增加返工次数。
本次 Handoff 也占消息预算，若新增它就会达到限制，在发布前拒绝。
预算在真正回合前再次检查；缺失 Token 仍记为缺失，不能将其计算为零成本。

## 明确未完成

这不是完整“继续执行”能力，不能连接真实模型的 HTTP/UI 入口：

- 同一个 service 的本地锁串行化本次操作；它不是持久化认领、多进程锁或崩溃恢复保证。
  锁覆盖整个回合，当前未纳入 API 的后台执行/取消调度；测试直接取消内部协程。
- Handoff、路由/ACK、预算与 Runtime 保存分属不同事务，存在部分写入窗口。
  成功后重复调用因原 Human 消息已 ACK 而拒绝，但没有可重放的持久化执行回执。
- 非零退出、非法输出或取消不自动重试，仍停在人工等待；已发布 Handoff/诊断不回滚。
  失败/取消回合的成本记账、运行中取消、崩溃后的重复执行防护仍待后续实现。
- 未核验历史测试结果对当前代码的时效性，不自动重跑测试；历史策略冻结/模型配置版本
  和全局 Reviewer 工具读取审计不属于本步。

下一子步骤实现持久化继续请求、原子认领/消费与幂等回执，再处理预算/失败恢复、受控
状态回归和 Fake 闭环验收。第 7 项才把人工输入及继续按钮接到 UI。

## 验证入口

```bash
.venv/bin/pytest -q tests/test_continuation_runtime.py tests/test_agent_turn_runner.py tests/test_workflow_execution.py tests/test_continuation_preflight.py tests/test_evidence_recovery.py tests/test_recovery_coordinator.py
.venv/bin/python scripts/check_offline.py
```

覆盖三个目标与 Orchestrator 转交、准备无写入、独立新会话、单回合不连锁派发、旧待办不
误消费、累计预算、Worktree/策略/注册表/Artifact 异常、异步检查时修订变化、失败/取消、
旧证据不可批准/返工、长人工正文、同进程重复尝试，以及注册表排队后的投递复检。
夹具明确在正常聊天室等待后将任务停为 `needs_human`；这不是新增生产自动暂停路径。
