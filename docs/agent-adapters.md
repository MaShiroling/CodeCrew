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

This boundary is not a substitute for the future `PermissionGate`, worktree path validation, or
post-run unauthorized-change detection.

## Tests

Unit tests use deterministic fake process handles and never contact providers. Live tests are
marked `integration`, skipped unless `CODECREW_RUN_CLI_INTEGRATION=1`, and use read-only prompts.
They can consume account quota and require authenticated CLI installations.

