# Unified execution tracing

`TraceStore` is CodeCrew's small, append-only execution timeline. It does not replace TeamRoom,
ArtifactStore, or their domain-specific records. Instead, it links those records into one ordered
task history suitable for diagnosis, recovery decisions, evaluation, and SSE delivery.

## Event envelope

Every `TraceEvent` contains a globally unique event ID, task and trace IDs, an event type, actor
kind and identity, a small JSON payload, an idempotency key, and an occurrence timestamp.
`correlation_id` groups a conversation or workflow chain. `causation_id` identifies the source
domain entity, such as the room message that caused a workflow decision. Large plans, diffs, logs,
and reports are never copied into the payload; Trace stores their Artifact IDs.

## Recorded events

The current runtime records:

- persisted TeamRoom messages and semantic Review or human-input events;
- WorkflowController decisions and resulting Task state changes;
- Agent Turn start, completion, reported Token usage, duration, and routed message IDs;
- failed Agent Turn attempts with bounded error summaries;
- deterministic Verification and CompletionGuard results with Artifact IDs;
- conversation-budget violations and their human escalation.
- recovery scan decisions, including resumable, waiting, needs-human, and terminal outcomes.

## Agent 回合诊断

`AgentTurnRunner` 在会话启动后收集 Adapter 已发出的规范化 `AgentEvent`。
回合结束、超时、取消或流式接口异常时，尝试写入 `agent-stream-<session_id>.json`
Generic Artifact（`metadata.purpose=agent-event-stream`），并追加 `agent_stream_recorded`。
Artifact 包含任务/Trace/会话 ID、原生会话 ID、角色、事件列表、`stream_complete` 和
`outcome`；保留收到的事件顺序、时间、文本、数据与 `native_event_type`，包括 stderr。
Trace 只引用 Artifact ID/哈希和事件数量，不内嵌 stderr 或工具参数。

`stream_complete` 仅表示 Adapter 的事件迭代器正常结束，**不代表任务或回合成功**。
例如超时进程可以正常关闭迭代器，此时 `stream_complete=true`、`outcome=timed_out`。
取消和接口异常保留已收到的前缀，不声称记录到取消后尚未消费的事件。流式接口异常
会尝试取消 Adapter，避免遗留运行进程；取消清理或诊断保存失败时保留原异常，并附
只含异常类别的说明。正常回合若诊断保存失败，继续抛错，不 ACK 或路由后续动作。

这是回合结束时的快照，不是逐事件落盘，也不是原生 stdout JSONL 的完整复制。
Adapter 未发出的原生事件不会凭空补齐；会话启动前失败、进程被强杀或机器崩溃，
不保证有诊断快照。此增量不改变 CLI 参数、180 秒联调超时、自动重试策略或成功条件。
原始 `AgentResult` 仍以独立的 `raw-agent-output` Artifact 保存；拿不到最终结果时只
保存事件前缀，不编造退出码、Token 或自然语言完成证据。

这两类诊断 Artifact 都会随真实冒烟证据归档。可通过归档的 `manifest.json` 中
`metadata.purpose` 查找对应 Artifact，再按 `sha256` 定位 Blob。原始日志可能含敏感
信息，分享前需检查；记录器不复制环境变量，也不承诺对任意模型回复自动脱敏。

## Reliability semantics

Writes are append-only. `(trace_id, idempotency_key)` is unique, and re-appending identical content
returns the original sequence. Reusing a key with different content is rejected. Query clients read
in monotonically increasing sequence order and can resume with `after_sequence`; this is the same
cursor contract the future SSE endpoint will expose.

Trace persistence and the domain write are currently separate SQLite transactions. Both are
idempotent, so recovery can safely backfill a missing Trace event from the authoritative domain
record. The future Recovery Coordinator will perform that reconciliation.
