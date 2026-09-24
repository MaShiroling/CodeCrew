"""Offline tests for the opt-in DeepSeek Reviewer smoke assertions."""

from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import AgentEvent, AgentEventType
from tests.integration.test_deepseek_reviewer_live import _checked_tool_calls, _snapshot


def _tool_event(name: str, file_path: str) -> AgentEvent:
    return AgentEvent(
        session_id=uuid4(),
        trace_id=uuid4(),
        sequence=0,
        type=AgentEventType.TOOL_CALL,
        data={"name": name, "input": {"file_path": file_path}},
    )


def test_live_check_requires_observable_approved_read_tool() -> None:
    assert _checked_tool_calls([_tool_event("Read", "/tmp/plan.json")]) == {
        "/tmp/plan.json"
    }
    with pytest.raises(AssertionError, match="no observable evidence"):
        _checked_tool_calls([])
    with pytest.raises(AssertionError, match="unapproved tool"):
        _checked_tool_calls([_tool_event("Bash", "/tmp/plan.json")])


def test_live_file_snapshot_detects_changes(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("value = 1\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    source.write_text("value = 2\n", encoding="utf-8")
    assert _snapshot(tmp_path) != before
    source.write_text("value = 1\n", encoding="utf-8")
    assert _snapshot(tmp_path) == before
    (tmp_path / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    assert _snapshot(tmp_path) != before
