from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents import (
    AgentArtifactInput,
    AgentEvent,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    PermissionMode,
    TokenUsage,
)


def test_request_keeps_provider_details_out_of_core_contract() -> None:
    task_id = uuid4()
    trace_id = uuid4()

    request = AgentRequest(
        task_id=task_id,
        trace_id=trace_id,
        role=AgentRole.PLANNER,
        prompt="Inspect the repository and return a structured plan.",
        working_directory=Path("/tmp/repository"),
    )

    assert request.permission_mode is PermissionMode.READ_ONLY
    assert request.timeout_seconds == 900
    assert request.resume_from_session_id is None


def test_request_rejects_empty_prompt_and_unknown_fields() -> None:
    common = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "role": AgentRole.REVIEWER,
        "working_directory": Path("/tmp/repository"),
    }

    with pytest.raises(ValidationError):
        AgentRequest(prompt="", **common)

    with pytest.raises(ValidationError):
        AgentRequest(prompt="Review the diff", provider="claude", **common)


def test_session_event_and_result_share_trace_identity() -> None:
    task_id = uuid4()
    trace_id = uuid4()
    session = AgentSession(
        task_id=task_id,
        trace_id=trace_id,
        agent_name="fake-planner",
        role=AgentRole.PLANNER,
    )
    event = AgentEvent(
        session_id=session.session_id,
        trace_id=trace_id,
        sequence=0,
        type=AgentEventType.STARTED,
    )
    result = AgentResult(
        session_id=session.session_id,
        trace_id=trace_id,
        reason=AgentExitReason.COMPLETED,
        exit_code=0,
        duration_ms=25,
    )

    assert event.session_id == result.session_id == session.session_id
    assert event.trace_id == result.trace_id == session.trace_id


def test_token_usage_requires_complete_known_pair_for_total() -> None:
    assert TokenUsage(input_tokens=12, output_tokens=8).total_tokens == 20
    assert TokenUsage(input_tokens=12).total_tokens is None


def test_event_sequence_and_token_counts_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        AgentEvent(
            session_id=uuid4(),
            trace_id=uuid4(),
            sequence=-1,
            type=AgentEventType.MESSAGE,
        )

    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1)


def test_artifact_grants_are_immutable_task_bound_unique_and_absolute(tmp_path: Path) -> None:
    task_id, trace_id = uuid4(), uuid4()
    values = {
        "artifact_id": uuid4(),
        "task_id": task_id,
        "trace_id": trace_id,
        "path": tmp_path / "plan.json",
        "sha256": "a" * 64,
        "size_bytes": 10,
    }
    grant = AgentArtifactInput(**values)
    request = {
        "task_id": task_id,
        "trace_id": trace_id,
        "role": AgentRole.IMPLEMENTER,
        "prompt": "Read approved plan",
        "working_directory": tmp_path,
    }
    assert AgentRequest(**request, artifact_inputs=(grant,)).artifact_inputs == (grant,)
    with pytest.raises(ValidationError, match="frozen"):
        grant.path = tmp_path / "other"
    with pytest.raises(ValidationError, match="absolute"):
        AgentArtifactInput(**{**values, "path": Path("relative.json")})
    with pytest.raises(ValidationError, match="unique"):
        AgentRequest(**request, artifact_inputs=(grant, grant))
    with pytest.raises(ValidationError, match="task and trace"):
        AgentRequest(
            **request, artifact_inputs=(AgentArtifactInput(**{**values, "trace_id": uuid4()}),)
        )
