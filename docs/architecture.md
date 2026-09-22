# MVP architecture

CodeCrew uses a deterministic orchestrator around replaceable agent adapters. Agents propose
and implement changes; deterministic code owns verification and completion.

## Runtime flow

1. The API creates a task from an issue and repository policy.
2. The orchestrator asks a read-only planner for a structured plan artifact.
3. An implementer works in a dedicated Git worktree and returns diff and test artifacts.
4. The verifier independently runs checks and records machine-readable evidence.
5. A separate read-only reviewer approves or requests rework.
6. The completion guard accepts only verifier evidence plus reviewer approval. Rework is capped
   by configuration and exhaustion moves the task to `needs_human`.

## Key contracts

- `AgentAdapter`: start a role-specific session, stream normalized events, cancel, and resume.
  Concrete Claude Code, Codex CLI, and fake adapters arrive in milestone two.
- `AgentRegistry`: capabilities, permissions, availability, and concurrency limits.
- `Orchestrator`: the sole writer of task state, applying explicit legal transitions.
- `AgentReviewerRunner`: starts a fresh read-only review session per attempt and validates its
  structured JSON result before the orchestrator persists it as evidence.
- `HandoffEnvelope`: versioned A2A message carrying small structured payloads and artifact IDs,
  with message ID, correlation ID, idempotency key, and acknowledgement state.
- `ArtifactStore`: immutable metadata and content-addressed blobs for plans, diffs, logs, and
  test results.
- `WorktreeManager`: creates and removes one isolated Git worktree per implementation attempt.
- `PermissionGate`: validates commands before execution and changed paths after execution.
- `Verifier`: produces deterministic build, public-test, hidden-test, and permission evidence.
- `CompletionGuard`: pure policy evaluation; agent prose is never evidence.
- `TraceStore`: append-only events sharing a task `trace_id` for replay and attribution.

## Dependency direction

Domain models have no process, database, or web dependencies. Adapters implement contracts
defined by the orchestration layer. SQLite repositories, subprocess runners, and FastAPI are
outer-layer implementations. This keeps fake adapters and in-memory stores usable in tests.

## Current exclusions

Planner output parsing, restart-safe task recovery, SSE streaming, the complete task API, and the
evaluation runner are not implemented yet. Reviewer rejection can trigger at most two bounded
rework rounds; budget exhaustion deterministically routes the task to `needs_human`.
