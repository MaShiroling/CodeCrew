# 验证证据恢复

`EvidenceRecoveryService` 使用 `TraceStore` 定位一个任务轨迹中最新的验证、评审和
完成守卫事件，再从 `ArtifactStore` 重建运行时对象。恢复结果可以直接写回
`WorkflowRuntime.latest_verification` 和 `latest_completion`，使重启后的控制器继续工作，
而不是重新相信 Agent 的自然语言结论。

恢复不是只读取最外层 JSON。服务会递归检查：

- Artifact 的 `task_id`、`trace_id`、类型、SHA-256 和字节长度；
- VerificationReport 引用的 ChangeSet、Diff、PermissionReport、CommandAudit 和测试日志；
- ReviewReport 与 CompletionDecision 内嵌的证据引用；
- `passed`、检查状态、权限违规和失败条件之间的一致性；
- CompletionDecision 是否晚于并准确引用当前最新的 VerificationReport 和 ReviewReport。

如果新的验证或评审发生在旧完成结论之后，旧结论会被视为过期而不注入运行时。
缺失、损坏、跨任务或跨 trace 的证据会终止恢复并抛出 `EvidenceRecoveryError`。

## Workflow Recovery Coordinator

`WorkflowRecoveryCoordinator.scan()` 分页扫描非终态任务，只读检查运行上下文、TeamRoom、
Worktree 和证据链，并记录 `RECOVERY_DECIDED` Trace 事件。它按以下规则分类：

- `resumable`：存在尚无 WorkflowController 决策的 Orchestrator 待处理消息；
- `waiting`：运行状态完整，但当前没有 Orchestrator 待处理消息；
- `needs_human`：上下文、房间、Worktree 或证据无效，或消息已产生状态决策但无法确认指令是否执行；
- `terminal`：任务已经完成、失败、取消或转人工。

`resume()` 仅接受 `resumable` 项，并在运行事件循环后保存 Task 状态与 Agent 原生会话 ID。
`recover_startup()` 提供启动时的扫描和安全续跑入口；Agent 执行失败后会转人工，避免自动重放
存在歧义的副作用。调用方仍需在应用生命周期中显式调用该入口，FastAPI 启动钩子留待后续集成。
