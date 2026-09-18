# CodeCrew

CodeCrew is an independent, open-source platform for orchestrating and evaluating
heterogeneous coding agents on software-change tasks.

The current implementation includes the project skeleton, task state model, provider-neutral
agent contracts, a deterministic fake adapter, an asynchronous process runner, an in-process
agent registry, and initial Claude Code and Codex CLI adapters. Worktree isolation,
deterministic verification, orchestration, and evaluation are planned work and are not yet
implemented.

## Requirements

- Python 3.11+
- Git
- Claude Code and Codex CLI are optional unless running live integration tests

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/uvicorn app.main:app --reload
```

Copy `.env.example` to `.env` to override development defaults. Secrets must be supplied
through environment variables and must not be committed.

Normal tests never call a live model. To explicitly run the optional CLI integration tests:

```bash
CODECREW_RUN_CLI_INTEGRATION=1 .venv/bin/pytest -m integration
```

These tests require installed, authenticated CLIs, network access, and may consume token quota.
See [docs/agent-adapters.md](docs/agent-adapters.md) for lifecycle and security details.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the MVP boundaries and contracts.

## License

Apache-2.0
