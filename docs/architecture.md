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

The event-driven collaboration layer adds a task-scoped `TeamRoom`. Agents exchange typed messages
through a `ConversationRouter`; SQLite stores the append-only conversation, per-recipient
acknowledgements, processed workflow decisions, and immutable Plan version chains. The
`WorkflowController` consumes these persisted events and remains the sole owner of legal Task
state transitions.

## Key contracts

- `AgentAdapter`: start a role-specific session, stream normalized events, cancel, and resume.
  Concrete Claude Code, Codex CLI, and fake adapters arrive in milestone two.
- `AgentRegistry`: capabilities, permissions, availability, and concurrency limits.
- `Orchestrator`: the sole writer of task state, applying explicit legal transitions.
- `TaskRepository`: stores detached Task snapshots in SQLite. Every save requires the caller's
  expected revision, preventing stale recovery workers from overwriting newer state.
- `TeamRoomStore`: durable rooms, members, messages, reply threads, cursors, recipient ACKs, and
  versioned Plan revisions linked to clarification questions.
- `ConversationRouter`: authenticates senders, enforces role routes and privileged message types,
  resolves recipients, and validates attached Artifact integrity before persistence.
- `AgentTurnRunner`: consumes pending room messages, leases a role-compatible adapter, captures
  normalized stream events, parses structured actions, routes them, and acknowledges inputs only
  after the complete turn succeeds. It also carries Plan and Review history into fresh independent
  sessions without copying the complete chat transcript.
- `WorkflowController`: idempotently reduces persisted room events into legal Task transitions and
  explicit directives to wake Agents, run Verifier or CompletionGuard, or request human input.
- `WorkflowDirectiveExecutor`: executes those directives and publishes Verifier and CompletionGuard
  results back into the room as integrity-bound system events.
- `WorkflowEventLoop`: feeds produced events back through the controller until completion, a human
  pause, an empty queue, or the configured event limit.
- `ConversationBudgetGuard`: persists per-session usage and deterministically escalates turn,
  Token, duration, message, repeated-content, and no-progress question limit violations to a human.
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

Task snapshots are durable, but reconstruction of runtime context and in-memory verification
evidence is not implemented yet. SSE streaming, the complete task API, and the evaluation runner
also remain future work. Reviewer rejection can trigger at most two bounded rework rounds; budget
exhaustion deterministically routes the task to `needs_human`.
