# Agent adapters

CodeCrew normalizes provider-specific CLIs behind `AgentAdapter`. Adapters report execution
facts; they do not decide whether a software-change task is successful.

## Lifecycle

1. `start(request)` launches a non-blocking local session.
2. `stream(session_id)` yields normalized events to one consumer.
3. `wait(session_id)` returns the cached final execution result.
4. `cancel(session_id)` requests termination and is safe to repeat.
5. `resume(native_session_id, request)` creates a new local session attached to provider state.

Every request, session, event, and result carries a `trace_id`. CodeCrew session IDs remain
separate from Claude session IDs and Codex thread IDs.

## Normalized events

- `started`: local process/session started.
- `stdout` and `stderr`: provider output that cannot be mapped more specifically.
- `message`: assistant text or provider diagnostic.
- `tool_call`: observed tool, command, or file-change event.
- `completed`, `failed`, and `cancelled`: execution outcome, not task completion evidence.

Token fields are nullable because not every provider event or failure path supplies usage data.

## Claude Code

`ClaudeCodeAdapter` is intentionally read-only for milestone two. It uses print mode with
streaming JSON, plan permissions, only `Read`, `Glob`, and `Grep`, safe mode, disabled slash
commands, and a strict empty MCP configuration. Requests for workspace-write permissions are
rejected before process launch.

`AgentReviewerRunner` acquires a `CODE_REVIEW`-capable adapter from the registry for each review,
always with `READ_ONLY` permission and a fresh session. It gives the reviewer the issue plus paths
to immutable Plan, Diff, changeset, and verification artifacts, then accepts only a schema-valid
JSON verdict. It never resumes the Planner session or treats reviewer prose as completion proof.

`DeepSeekClaudeReviewerAdapter` is an explicit Reviewer-only variant of this read-only harness.
When selected with `reviewer_adapter: "deepseek-claude-reviewer"`, it requires a nonempty
`DEEPSEEK_API_KEY` environment variable, sets the official DeepSeek Anthropic-compatible endpoint
and Flash model **only in the Reviewer child process**, and does not pass the key on the command
line. The child receives a small allowlist of inherited environment variables rather than other
providers' credentials. This is an adapter and offline-tested configuration path, not yet a live
provider authentication or model-identity test.

Kimi Code CLI is present locally and is the intended Implementer harness. Its `-p` mode performs
tool calls without human approval. Kimi's official Hooks are fail-open on error/timeout, so they
cannot be the sole command/path security barrier. `KimiCodeAdapter` is an **explicit opt-in**
Implementer: it checks the task-owned worktree, gives each turn a fresh private `HOME` and
`KIMI_CODE_HOME`, injects `KIMI_MODEL_API_KEY` only in the child environment, and launches the
CLI inside macOS `KimiWriteBoundary`. Its packaged agent file exposes only
`Read/Grep/Glob/Write/Edit`; CodeCrew's `CommandExecutor`, not Kimi's `Bash`, runs tests.
Unexpected JSONL tool names or malformed output fail the turn. The write boundary has system-level
tests for permitted paths, denied repository metadata, outside paths, and symlink escape; the actual
Kimi binary has been started under it with `--version` only. **A real model turn has not yet verified
the CLI tool restriction.** Kimi's documented `--agent-file`/`--session` incompatibility means
native resume is disabled; structured mailbox messages start a fresh session on subsequent turns.
See [the integration decision](real-model-integration.md).

## Codex CLI

`CodexCliAdapter` uses non-interactive `exec --json`. Permission mode maps to the CLI's
`read-only` or `workspace-write` sandbox. Approval policy is `never`, so an unattended run fails
instead of waiting for input. The adapter never uses the dangerous sandbox bypass option. User
configuration and local execution policy rules are ignored for repeatability; repository agent
instructions can still apply.

OpenAI's current Codex developer documentation lists non-interactive mode as the automation
surface: <https://developers.openai.com/codex/cli/reference>.

## Process boundary

Commands are passed as argument arrays to `asyncio.create_subprocess_exec`; no shell is used.
stdin is closed with `DEVNULL`. stdout and stderr are read concurrently. Output buffering is
bounded, and processes are terminated then killed after a configured grace period when needed.
If an adapter supplies `env`, that mapping is the **complete** child environment, not an overlay
on the parent; adapters needing isolation must explicitly pass the required non-secret runtime
variables. With `env=None`, the child retains the normal inherited environment.

This boundary is not a substitute for the future `PermissionGate`, worktree path validation, or
post-run unauthorized-change detection.

## Tests

Unit tests use deterministic fake process handles and never contact providers. Live tests are
marked `integration`, skipped unless `CODECREW_RUN_CLI_INTEGRATION=1`, and use read-only prompts.
They can consume account quota and require authenticated CLI installations.
