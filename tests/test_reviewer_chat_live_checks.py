"""Exercise live-entry wiring with simulated CLI processes, never real models."""

import json
from types import SimpleNamespace

import pytest

from app.agents import DeepSeekClaudeReviewerAdapter
from app.team import ChatActionError
from scripts.replay_reviewer import replay_archive
from scripts.smoke_evidence import archive_smoke_evidence
from tests.integration import test_reviewer_chat_live as live
from tests.test_reviewer_chat_smoke import NativeReviewerProcess


def configure_entry(monkeypatch, tmp_path, process, *, archive_failure=False):
    preflight_calls, archives = [], []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-live-entry-placeholder")
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "offline-kimi-not-forwarded")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-openai-not-forwarded")

    def which(name):
        assert name == "claude", "Reviewer-only entry must not check Codex/Kimi"
        return "/offline/claude"

    monkeypatch.setattr(live.shutil, "which", which)
    monkeypatch.setattr(
        live, "_preflight", lambda executable, root: preflight_calls.append(executable)
    )
    monkeypatch.setattr(
        live,
        "DeepSeekClaudeReviewerAdapter",
        lambda **kwargs: DeepSeekClaudeReviewerAdapter(runner=process, **kwargs),
    )

    def archive(store, task, *, root):
        assert root.name.startswith("reviewer-chat-")
        if archive_failure:
            raise ValueError("private archive exception must not be printed")
        path = archive_smoke_evidence(store, task, root=tmp_path / "archives")
        archives.append(path)
        return path

    monkeypatch.setattr(live, "archive_smoke_evidence", archive)
    return preflight_calls, archives


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,count", [("approval", 1), ("rework", 2)])
async def test_live_entry_archives_native_evidence_without_full_chain(
    tmp_path, monkeypatch, capsys, scenario, count
):
    process = NativeReviewerProcess()
    preflight, archives = configure_entry(monkeypatch, tmp_path, process)
    await live.run_live_reviewer_chat(tmp_path, scenario=scenario)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 2 and lines[0]["archive_integrity_verified"]
    assert lines[1]["reviewer_acceptance_passed"] and not lines[1]["task_completion_evaluated"]
    assert lines[1]["reviewer_turns"] == count and len(process.calls) == count
    assert preflight == ["/offline/claude"]
    assert len(replay_archive(archives[0], source="native")["cases"]) == count
    for _, options in process.calls:
        assert options["env"]["HOME"].startswith(str(tmp_path / "reviewer-chat-home-"))
    assert all("offline-live-entry-placeholder" not in str(line) for line in lines)


@pytest.mark.asyncio
async def test_failed_native_entry_still_archives_raw_output_and_failure_trace(
    tmp_path, monkeypatch, capsys
):
    process = NativeReviewerProcess("missing_report")
    _, archives = configure_entry(monkeypatch, tmp_path, process)
    with pytest.raises(ChatActionError):
        await live.run_live_reviewer_chat(tmp_path, scenario="approval")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1 and lines[0]["archive_integrity_verified"]
    assert "reviewer_acceptance_passed" not in lines[0]
    replay = replay_archive(archives[0], source="native")
    assert replay["cases"][0]["decision"] == "rejected" and len(process.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,error", [("normal", ValueError), ("missing_report", ChatActionError)]
)
async def test_archive_failure_never_masks_original_error_or_prints_acceptance(
    tmp_path, monkeypatch, capsys, mode, error
):
    configure_entry(monkeypatch, tmp_path, NativeReviewerProcess(mode), archive_failure=True)
    with pytest.raises(error):
        await live.run_live_reviewer_chat(tmp_path, scenario="approval")
    printed = capsys.readouterr().out
    assert json.loads(printed)["archive_integrity_verified"] is False
    assert "private archive exception" not in printed
    assert "reviewer_acceptance_passed" not in printed


@pytest.mark.parametrize(
    "help_text,exit_code,valid",
    [
        ("", 0, False),
        ("--json-schema", 1, False),
        (
            (
                "--print --verbose --output-format --safe-mode --disable-slash-commands --strict-mcp-config "
                "--mcp-config --permission-mode --tools --json-schema"
            ),
            0,
            True,
        ),
    ],
)
def test_native_cli_preflight_checks_flags_without_starting_a_model(
    tmp_path, monkeypatch, help_text, exit_code, valid
):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["/offline/claude", "--help"]
        return SimpleNamespace(stdout=help_text, returncode=exit_code)

    monkeypatch.setattr(live.subprocess, "run", run)
    if valid:
        live._preflight("/offline/claude", tmp_path)
    else:
        with pytest.raises(RuntimeError):
            live._preflight("/offline/claude", tmp_path)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["cli", "key"])
async def test_missing_preconditions_never_start_agent(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(
        live.shutil, "which", lambda name: None if missing == "cli" else "/offline/claude"
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        live,
        "_preflight",
        lambda *args: pytest.fail("must not preflight before required credentials"),
    )
    with pytest.raises(pytest.fail.Exception, match="CLI is not on PATH" if missing == "cli" else "DEEPSEEK_API_KEY"):
        await live.run_live_reviewer_chat(tmp_path, scenario="approval")
