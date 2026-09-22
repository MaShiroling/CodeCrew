# TeamRoom conversation execution

CodeCrew's TeamRoom is a controlled task conversation, not an unrestricted group chat. Every
message is task- and trace-bound, every sender is authenticated as a room member, and attached
Artifacts are integrity checked before persistence.

## Structured actions

An Agent turn returns one JSON object containing up to 20 actions. Supported actions are:

- `send_message`, `ask_question`, and `answer_question`;
- `share_artifact` and `report_progress`;
- `request_review`, `request_rework`, and `request_human_input`;
- `finish_turn`.

Exactly one `finish_turn` must appear at the end. Answers require `reply_to`; Artifact sharing
requires Artifact IDs. Unknown fields and prose outside the JSON object are rejected.

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
The controller currently produces directives; automatic execution of those directives is the next
integration step.
