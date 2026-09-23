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
下一步的 Recovery Coordinator 将负责启动扫描、任务分级和安全续跑策略。
