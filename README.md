# CodeCrew

CodeCrew is an independent, open-source platform for orchestrating and evaluating
heterogeneous coding agents on software-change tasks.

The first milestone contains only the project skeleton, configuration, task state model,
and tests. Agent process integration, worktree isolation, deterministic verification, and
evaluation are planned work and are not yet implemented.

## Requirements

- Python 3.11+
- Git

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/uvicorn app.main:app --reload
```

Copy `.env.example` to `.env` to override development defaults. Secrets must be supplied
through environment variables and must not be committed.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the MVP boundaries and contracts.

## License

Apache-2.0

