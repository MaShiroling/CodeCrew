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

## Reliability semantics

Writes are append-only. `(trace_id, idempotency_key)` is unique, and re-appending identical content
returns the original sequence. Reusing a key with different content is rejected. Query clients read
in monotonically increasing sequence order and can resume with `after_sequence`; this is the same
cursor contract the future SSE endpoint will expose.

Trace persistence and the domain write are currently separate SQLite transactions. Both are
idempotent, so recovery can safely backfill a missing Trace event from the authoritative domain
record. The future Recovery Coordinator will perform that reconciliation.
