"""One opt-in, bounded Kimi Code membership smoke test.

Run only from a terminal with a newly rotated KIMI_MODEL_API_KEY. The test never
prints the key or raw CLI/wire output and removes the private CLI runtime.
"""

import asyncio
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest

import app.storage  # noqa: F401 - initialize the existing workspace import graph.
from app.agents import (
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    KimiCodeAdapter,
    PermissionMode,
)
from app.workspace import PermissionPolicy, WorktreeManager

pytestmark = pytest.mark.integration

_ALLOWED_TOOLS = {"Read", "Grep", "Glob", "Write", "Edit"}
_SMOKE_CONTENT = "CODECREW_KIMI_SMOKE"


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _git_status(directory: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.splitlines()


def _valid_smoke_content(content: str | None) -> bool:
    """A single line may be terminated by one LF or by end-of-file."""
    return content in {_SMOKE_CONTENT, f"{_SMOKE_CONTENT}\n"}


def _tool_names(value: Any) -> set[str] | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"activeToolNames", "tools", "tool_schemas", "toolSchemas"} and isinstance(
                nested, list
            ):
                names = {
                    item if isinstance(item, str) else item.get("name")
                    for item in nested
                    if isinstance(item, (str, dict))
                }
                names.discard(None)
                if names:
                    return {name for name in names if isinstance(name, str)}
            found = _tool_names(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _tool_names(item)
            if found is not None:
                return found
    return None


def _effective_tool_names(kimi_home: Path) -> tuple[set[str] | None, set[str]]:
    """Read only schema metadata from private wire records; never return raw content."""
    record_types: set[str] = set()
    candidates: dict[str, set[str]] = {}
    for wire in (kimi_home / "sessions").rglob("wire.jsonl"):
        if wire.parent.parent.name != "agents":
            continue
        with wire.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = record.get("type")
                if not isinstance(record_type, str):
                    continue
                record_types.add(record_type)
                if record_type in {"llm.tools_snapshot", "llm.request", "profile.bind"}:
                    names = _tool_names(record)
                    if names:
                        candidates.setdefault(record_type, set()).update(names)
    for record_type in ("llm.request", "llm.tools_snapshot", "profile.bind"):
        if record_type in candidates:
            return candidates[record_type], record_types
    return None, record_types


def test_tool_schema_probe_reads_only_names(tmp_path: Path) -> None:
    wire = tmp_path / "sessions" / "workdir" / "session" / "agents" / "custom-agent" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text(
        json.dumps(
            {
                "type": "llm.tools_snapshot",
                "tools": [{"name": "Read"}, {"name": "Write"}],
                "private_payload": "never returned by the probe",
            }
        ) + "\n",
        encoding="utf-8",
    )
    assert _effective_tool_names(tmp_path) == (
        {"Read", "Write"}, {"llm.tools_snapshot"}
    )


def test_tool_schema_probe_unions_all_actual_requests(tmp_path: Path) -> None:
    wire = tmp_path / "sessions" / "workdir" / "session" / "agents" / "custom-agent" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "llm.tools_snapshot", "tools": [{"name": "Glob"}]},
                {"type": "llm.request", "tools": [{"name": "Read"}]},
                {"type": "llm.request", "tools": [{"name": "Bash"}]},
            )
        ) + "\n",
        encoding="utf-8",
    )
    assert _effective_tool_names(tmp_path)[0] == {"Read", "Bash"}


@pytest.mark.parametrize(
    ("content", "valid"),
    [
        (_SMOKE_CONTENT, True),
        (f"{_SMOKE_CONTENT}\n", True),
        (f"{_SMOKE_CONTENT}\n\n", False),
        (f" {_SMOKE_CONTENT}", False),
        (None, False),
    ],
)
def test_smoke_content_accepts_one_line_with_optional_final_lf(
    content: str | None, valid: bool
) -> None:
    assert _valid_smoke_content(content) is valid


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_KIMI_LIVE") != "1",
    reason="set CODECREW_RUN_KIMI_LIVE=1 to spend one Kimi Code model turn",
)
async def test_live_kimi_restricted_file_edit(tmp_path: Path) -> None:
    if platform.system() != "Darwin":
        pytest.fail("Kimi live smoke requires macOS Seatbelt")
    if shutil.which("kimi") is None:
        pytest.fail("Kimi CLI is not on PATH")
    if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        pytest.fail("set KIMI_MODEL_API_KEY in this terminal; do not put it in the command")

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    source = repository / "src"
    source.mkdir()
    (source / "README.txt").write_text("Temporary Kimi smoke fixture\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=CodeCrew Smoke", "-c", "user.email=smoke@codecrew.invalid", "commit", "-m", "fixture")

    task_id = uuid4()
    manager = WorktreeManager(tmp_path / "worktrees")
    handle = await manager.create(task_id=task_id, repository=repository)
    try:
        with TemporaryDirectory(prefix="kimi-smoke-", dir=tmp_path) as private_dir:
            runtime_root = Path(private_dir) / "runtime"
            adapter = KimiCodeAdapter(
                worktree_root=manager.root,
                runtime_root=runtime_root,
                policy=PermissionPolicy(allowed_paths=("src",)),
                max_steps_per_turn=8,
            )
            request = AgentRequest(
                task_id=task_id,
                trace_id=uuid4(),
                role=AgentRole.IMPLEMENTER,
                prompt=(
                    "In this temporary Git worktree, use the Write or Edit tool to create "
                    "src/kimi_smoke.txt with exactly one line: CODECREW_KIMI_SMOKE. "
                    "A final newline is optional. "
                    "Do not run shell commands or edit any other file. "
                    "When done, reply with CODECREW_KIMI_SMOKE_DONE."
                ),
                working_directory=handle.worktree_path,
                permission_mode=PermissionMode.WORKSPACE_WRITE,
                timeout_seconds=120,
            )
            session = await adapter.start(request)
            events = [event async for event in adapter.stream(session.session_id)]
            result = await adapter.wait(session.session_id)

            stderr = "".join(
                event.text or "" for event in events if event.type is AgentEventType.STDERR
            )
            error_class = next(
                (status for status in ("401", "403", "429") if status in stderr), "other"
            )
            meta_types = sorted({
                event.data["meta_type"]
                for event in events
                if event.type is AgentEventType.STDOUT and "meta_type" in event.data
            })
            assert result.reason is AgentExitReason.COMPLETED, (
                f"Kimi turn failed: exit={result.exit_code}, category={error_class}, "
                f"meta_types={meta_types}"
            )
            target = handle.worktree_path / "src" / "kimi_smoke.txt"
            content = (
                target.read_text(encoding="utf-8")
                if target.is_file() and not target.is_symlink()
                else None
            )
            changed_files = await asyncio.to_thread(_git_status, handle.worktree_path)
            observed_calls = {
                event.data.get("name")
                for event in events
                if event.type is AgentEventType.TOOL_CALL
            }
            private_home = runtime_root / str(task_id) / str(session.session_id) / "kimi-home"
            available_tools, record_types = _effective_tool_names(private_home)
            failures: list[str] = []
            if "CODECREW_KIMI_SMOKE_DONE" not in result.output.get("message", ""):
                failures.append("missing completion marker")
            if not _valid_smoke_content(content):
                failures.append("target file does not contain the exact single smoke line")
            if changed_files != ["?? src/kimi_smoke.txt"]:
                failures.append("Git changes are not limited to the target file")
            if not observed_calls <= _ALLOWED_TOOLS:
                failures.append("a forbidden tool was called")
            if not observed_calls.intersection({"Write", "Edit"}):
                failures.append("no Write/Edit call was observed")
            if available_tools is None:
                failures.append(
                    "no verifiable tool schema in Kimi wire; "
                    f"record types: {sorted(record_types)}"
                )
            elif not available_tools <= _ALLOWED_TOOLS:
                failures.append("Kimi exposed a forbidden tool")
            assert not failures, "; ".join(failures)
    finally:
        await manager.remove(task_id, force=True)
