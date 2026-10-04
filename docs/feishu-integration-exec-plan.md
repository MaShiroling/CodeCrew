# Feishu v1 ExecPlan

## Scope and frozen decisions

This is an implementation plan, updated during execution. D3: allowlisted DM text
and allowlisted group text with an actual mention of this bot's open ID. D4: one
application bot, three labelled internal agents. D5: discussion only; external
Human messages cannot authorize coding, including through the local web UI.
D6: one stable room per `(app_id, chat_id)`, no thread switching.

## F0 — baseline and implementation map

- [x] Read product/development specs, README and architecture. No repository
  AGENTS.md or CLAUDE.md exists in the source snapshot.
- [x] Create independent development checkout from upstream `54d4767`; there
  were no user edits here. The original downloaded snapshot remains unchanged.
- [x] Confirm official optional SDK package and inspect its actual lifecycle.
- [x] Run baseline checks in this Windows environment; distinguish existing
  macOS/Unix assumptions from new regressions.

Existing chat stores JSON messages and fingerprints in SQLite; migrations 15,
16 and 18 belong to standalone chat, 17 to coding authorization. Root messages
uniquely identify persisted bounded runs. The legacy dispatcher and bounded
dispatcher already fence uncertain turns on startup. They must remain the only
agent orchestrators. SDK callbacks must never invoke either synchronously.

## F1 — contracts, configuration and SDK boundary

- [x] Add strict FeishuInbound, safe identifiers and parser dispositions.
- [x] Validate event type, sender, text JSON and genuine mentions; fail closed.
- [x] Optional `lark-oapi` extra; isolate SDK objects inside sender/transport.
- [x] SecretStr settings, JSON-array allowlists, bounded retry configuration.
- [x] Fake transport/sender and explicit opt-in live probe.

## F2 — external identity and durable ingress

- [x] Optional validated external_source on Human messages, API/UI/context
  projection; omit it from old fingerprints when absent, ignore display names
  in external replay fingerprints. Keep four-member room model.
- [x] Restricted external-message service method using existing persona aliases.
- [x] Migration 19 (after verifying no conflict): bindings, ingress identities,
  outbox and indexes; no credentials or raw SDK events.
- [x] Stable binding/message/run identities and claim-before-dispatch ingress.
- [x] Busy arbitration based on durable run state and a single-process lock.

Inbound: parse -> both allowlists -> durable identity claim -> stable binding ->
external Human message -> persist correlation/run -> schedule existing bounded
dispatcher. Replay resumes bookkeeping, never schedules an existing uncertain run.
Do not mix a second ingress into an active run. Save a deduplicated busy notice.

## F3 — bounded discussion and authorization boundary

- [x] Reuse existing bounded dispatcher/store/runtime; no parallel fanout.
- [x] Preserve existing context/turn/time limits, A -> B -> A and role aliases.
- [x] Disallow external sources in coding preflight and authorization replay.
- [x] Correlation isolation for prompts, web-local messages and external output.

## F4 — outbox, restart and delivery

- [x] Scan persisted messages with binding start sequence + ingress correlation
  + Agent sender; transactionally insert outbox rows and advance scan cursor.
- [x] Sequence ordering, stable source keys, terminal/busy status notices.
- [x] Durable pending/sending/retry_wait/sent/failed, bounded exponential retry,
  stale sending recovery, safe errors and receipt IDs. Never rerun an Agent.
- [x] Stop ingress before shutdown, stop bounded work using existing lifecycle;
  next startup fences old runs and scans all missing messages/statuses.

Crash windows to test: room created before binding; Human persisted before
ingress finalized; run persisted before scheduling; Agent persisted before scan;
outbox sending before/after remote send. SQLite + Feishu cannot provide exactly
once: remote success before receipt commit can duplicate delivery. Document
durable at-least-once delivery with local dedupe, finite retries and diagnostics.

## F5 — lifecycle, UI, documentation and acceptance

- [x] Only `chat-serve --feishu` plus enabled/config validation may mount runtime.
- [x] Lifespan startup: chat -> bounded -> Feishu; reverse shutdown, including
  startup failure cleanup. Single process only, no webhook.
- [x] Safe read-only status endpoint, disabled response without runtime.
- [x] External sender label distinct from local “我”, no coding button; minimal
  connection/delivery UI without a settings console.
- [x] Setup guide, frozen specs, README, env example and architecture update.
- [ ] Full offline regression comparison (running); named key regressions, Ruff and self-review done.

## Test matrix and review gates

Parser: DM/group, real/fake/all/other mentions, self echo, nontext, malformed and
missing fields. Admission: empty/unauthorized chat/sender without room/model.
Identity: A/B labels in API/UI/prompts, backward-compatible old messages.
Replay: duplicate event/message, concurrent initial ingress, restart and every
crash window. Runs: one root/run, busy isolation, repeated role and budgets.
Outbox: ordered three-role replies, local/cross-correlation exclusion, repeat
scan, restart, two failures then success, retry exhaustion and stale sends.
Security: no Task/worktree/shell/repository access, web authorization rejects
external messages, safe logs/status/errors, no eager SDK import. Lifecycle:
optional CLI behavior, callback thread handoff, reconnect and stop cleanup.

## Risks and unverified work

- Real Feishu credentials and console are not available: live connection, DM,
  group mention and send acceptance remain PENDING until explicitly exercised.
- Current host is Windows; baseline has os.getuid/macOS sandbox assumptions.
  Do not weaken production isolation or disguise platform failures as passes.
- SDK 1.7.3 has no public stop; inspected source and kept the
  version-specific boundary narrow, tested and documented.
- Transport ingress is a bounded memory handoff, not an infinite durable queue;
  overload must be observable and must not be described as lossless reception.
- Remote text is untrusted. Outbound uses only persisted Agent content and fixed
  status strings, with conservative secret/path filtering and no raw stderr.

## Execution log

- F0: specs and existing storage/service contracts inspected; migration 19 is
  reserved for this feature after repository-wide migration inspection.

- F1–F5: bridge, optional official SDK transport, durable recovery/outbox, UI,
  coding provenance checks and Fake tests implemented. SDK contract tests passed
  without network. Real Feishu smoke remains PENDING, not a pass.
- Self-review: fixed disabled-binding delivery, process-start cleanup, malformed
  queue state, closed-room recovery and unsafe display-name controls.
- Windows baseline: 1351 tests; 208 failures, 11 errors, 24 skips. Named
  regressions: 6 passes / 4 pre-existing failures. No isolation weakened.
