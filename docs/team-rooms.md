# TeamRoom conversation execution

CodeCrew's TeamRoom is a controlled task conversation, not an unrestricted group chat. Every
message is task- and trace-bound, every sender is authenticated as a room member, and attached
Artifacts are integrity checked before persistence.

## Structured actions

An Agent turn returns one JSON object containing up to 20 actions. Supported actions are:

- `send_message`, `ask_question`, and `answer_question`;
- `share_artifact`, `share_plan`, and `report_progress`;
- `request_review`, `approve_review`, `request_rework`, and `request_human_input`;
- `finish_turn`.

Exactly one `finish_turn` must appear at the end. Answers require `reply_to`; Artifact sharing
requires Artifact IDs. Unknown fields are rejected. Chat turns accept raw JSON or one
explicitly `json`-labelled fenced block with optional surrounding prose. Prose is never
an action or completion evidence. Multiple/other/unbalanced fences, extra JSON object/array
candidates outside the block, duplicate keys, and non-JSON constants are rejected. Wrapper
prose is bounded to 16,000 characters and 100 possible object/array starts. Schema, role,
recipient, Artifact ownership and CompletionGuard checks remain mandatory.

Once a matching-trace AgentResult is available, the full result (including the exact output
string before normalization) is saved as a diagnostic Generic Artifact with purpose
`raw-agent-output`. An `agent_output_recorded` Trace event references its ID and SHA-256;
both accepted and rejected replies are recorded. This recording is not a message ACK,
an action Artifact, or proof of success. Cancellation before a result exists cannot record
a final result. The standalone legacy Reviewer verdict parser does not opt into prose wrappers.

## Planner clarification and Plan versions

An Implementer can send `ask_question` to the Planner without changing the task's
`implementing` state. The question wakes only its resolved recipient. The Planner answers with an
`answer_question` action bound to the original message through `reply_to`, then may publish a
clarified `share_plan` action in the same turn.

Every accepted `plan_shared` message creates an immutable `PlanRevision` row. Version 1 has no
parent. Later versions must supersede the latest Plan Artifact and identify one or more persisted
questions in the same room through `addresses_message_ids`. `AgentTurnRunner` derives these links
from the latest Plan and the pending questions when the Planner does not repeat them explicitly.
The prompt includes the complete lightweight Plan history while Plan bodies remain Artifact
references. Revised Plans keep the task in `implementing` and wake the Implementer again.

## Reviewer rework conversation

A Reviewer rejects an implementation with `request_rework`, targeting the Implementer and
attaching exactly one `ReviewReport` Artifact. Inline reports are normalized into the shared
`ReviewIssue` contract. A rejected report must contain at least one unresolved issue, and the
Router independently verifies that the Artifact is task/trace bound, has a `rejected` verdict, and
contains actionable unresolved issues.

The rejection event consumes one deterministic rework round, moves the task back to
`implementing`, and wakes the Implementer with the Review Artifact path in its prompt. A subsequent
`request_review` always returns through Verifier before a fresh Reviewer turn. Review history is
included as lightweight structured data plus Artifact paths. Stable `issue_id` values form the
cross-round ledger: every still-unresolved issue must be carried into the next Review report, and a
Reviewer cannot approve while a carried high or critical issue remains unresolved. The existing
rework budget still routes the task to a human after the configured limit.

## Conversation budgets and loop detection

`ConversationBudgetGuard` checks limits immediately before every Agent wake-up. Successful turns
are recorded idempotently in SQLite with session, role, reported input/output Token usage, and
duration, so a process restart does not reset the budget. Room messages remain the source of truth
for message-count and loop checks.

The policy bounds total Agent turns, reported Tokens, cumulative Agent duration, and room messages.
It also fingerprints normalized question, answer, and ordinary-message content to detect repeated
speech, and counts questions since the most recent concrete workflow-progress event. Unknown Token
usage is tracked separately and is never guessed; turn and duration limits still apply.

When a limit is reached, the next Agent is not started. The executor moves the active task to
`needs_human`, publishes a protected `human_input_request` containing the violated limit and
observed value, and pauses the event loop. These limits are configured through `CODECREW_*`
environment variables documented in `.env.example`.

## Turn lifecycle

`AgentTurnRunner` performs one bounded turn:

1. Read pending messages for one Agent member using a configured limit.
2. Build a prompt containing the issue, room roster, new messages, and Artifact paths.
3. Acquire a compatible adapter and enforce role permissions: Planner and Reviewer are read-only;
   Implementer uses workspace-write.
4. Start a fresh session or explicitly resume a provider-native session.
5. Capture normalized stream events and require a successful process result.
6. Parse and route every action through `ConversationRouter`.
7. ACK the consumed messages only after all actions are accepted.

If the provider fails, output parsing fails, or routing rejects an action, input messages remain
pending. Routed actions use deterministic idempotency keys derived from their input messages, so a
retry cannot silently duplicate or change an already persisted action.

## Reliability boundary

Chat actions coordinate work but never complete a task. Task state remains controlled by the
workflow layer, while Verifier and CompletionGuard remain the only sources of completion evidence.

## Event-driven workflow control

`WorkflowController` consumes only messages already persisted by `ConversationRouter`. Explicit
events advance the task: issue posting wakes Planner, Plan sharing wakes Implementer,
implementation readiness schedules Verifier, verification evidence wakes Reviewer, and review
approval schedules CompletionGuard. Questions and answers wake their concrete recipients without
changing task state.

Each processed message and its decision are persisted. Re-delivery returns the same decision and
can replay an unapplied transition after a narrow process interruption. Invalid event ordering is
rejected. Rework events consume the configured budget and eventually emit a human-input directive.

## Directive execution and event loop

`WorkflowDirectiveExecutor` connects controller output to runtime components:

- Agent wake directives call `AgentTurnRunner` with role-specific permissions.
- Verifier directives run deterministic checks and publish `verification_ready` from the protected
  Verifier system member.
- Completion directives load the bound Review artifact, evaluate `CompletionGuard`, and publish a
  protected pass or rejection event.
- Human directives pause automatic execution without acknowledging away the required input.

`WorkflowEventLoop` queues every message produced by these actions and sends it back through the
controller. Replayed controller decisions do not rerun expensive directives, and messages delivered
to the Orchestrator are acknowledged only after their directives succeed. A configurable event
limit prevents an unbounded local run.

If one Agent turn emits several messages for the same recipient, their directives may request more
than one wake-up. The executor coalesces later wake-ups after the first turn consumes the complete
pending batch; an empty mailbox is therefore not treated as an Agent failure.

Planner and Reviewer actions may include a small JSON `artifact_content`. TurnRunner persists this
content as a Plan or Review Artifact before routing the message. Large patches and logs must still
use existing Artifact IDs. The current runtime keeps the latest structured VerificationReport in
memory; reconstructing that complete object after a process restart remains future work.
