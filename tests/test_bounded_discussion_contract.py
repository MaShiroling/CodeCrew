"""P6.1: contracts only, before persistence, HTTP controls or Agent dispatch."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.chat.bounded_dispatch import _parse_discussion_reply
from app.chat.discussion_runs import (
    DiscussionNextAction,
    DiscussionReply,
    DiscussionRun,
    DiscussionRunLimits,
    DiscussionRunStatus,
    DiscussionStopReason,
    reserve_discussion_turn,
    transition_discussion_run,
)
from app.team.models import MemberRole

_START = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("wrapper", [
    "{}",
    "```json\n{}\n```",
    "我已整理建议。\n```json\n{}\n```",
    "```json\n{}\n```\n补充说明。",
    "建议如下：\n{}",
])
def test_bounded_reply_accepts_one_unambiguous_json_presentation(wrapper: str) -> None:
    payload = {
        "content": "月见：补充实现视角。",
        "next_action": "handoff",
        "handoff_to": ["reviewer"],
    }
    parsed = _parse_discussion_reply(
        {"message": wrapper.format(json.dumps(payload, ensure_ascii=False))},
        speaker=MemberRole.IMPLEMENTER,
    )
    assert parsed == DiscussionReply.model_validate(payload)


@pytest.mark.parametrize("raw", [
    "只有自然语言，没有决定",
    (
        '说明\n```json\n{"content":"建议","next_action":"finish","handoff_to":[]}\n```\n'
        '```json\n{"content":"建议","next_action":"finish","handoff_to":[]}\n```'
    ),
    '{"content":"a","content":"b","next_action":"finish","handoff_to":[]}',
    '{"content":"a","next_action":"handoff","handoff_to":["human"]}',
    '{"content":"a","next_action":"handoff","handoff_to":["implementer"]}',
    '{"content":"a","next_action":"finish","handoff_to":[],"write_file":"x"}',
])
def test_bounded_reply_rejects_ambiguous_or_unauthorized_json(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid discussion"):
        _parse_discussion_reply({"message": raw}, speaker=MemberRole.IMPLEMENTER)


def run(**changes) -> DiscussionRun:
    values = {
        "room_id": uuid4(),
        "root_message_id": uuid4(),
        "correlation_id": uuid4(),
        "opening_role": MemberRole.PLANNER,
        "created_at": _START,
        "updated_at": _START,
    }
    return DiscussionRun.model_validate(values | changes)


def test_run_defaults_are_bounded_and_have_no_coding_authority() -> None:
    created = run()
    assert created.status is DiscussionRunStatus.CREATED
    assert created.stop_reason is None
    assert created.agent_turns_used == 0
    assert created.limits == DiscussionRunLimits(max_agent_turns=8, max_elapsed_seconds=600)
    assert created.started_at is None and created.stopped_at is None
    assert "task_id" not in created.model_fields_set
    assert DiscussionRun.model_validate_json(created.model_dump_json()) == created

    with pytest.raises(ValidationError, match="extra_forbidden"):
        run(repository_path="/tmp/repo")
    with pytest.raises(ValidationError, match="opening role must be an Agent"):
        run(opening_role=MemberRole.HUMAN)
    with pytest.raises(ValidationError, match="less than or equal to 8"):
        DiscussionRunLimits(max_agent_turns=9)
    with pytest.raises(ValidationError, match="less than or equal to 600"):
        DiscussionRunLimits(max_elapsed_seconds=601)


def test_pause_resume_retains_turn_count_and_original_deadline() -> None:
    created = run(limits=DiscussionRunLimits(max_agent_turns=3, max_elapsed_seconds=60))
    running = transition_discussion_run(
        created, DiscussionRunStatus.RUNNING, at=_START + timedelta(seconds=1),
    )
    one_turn = reserve_discussion_turn(running, at=_START + timedelta(seconds=2))
    paused = transition_discussion_run(
        one_turn, DiscussionRunStatus.PAUSED,
        reason=DiscussionStopReason.HUMAN_PAUSED,
        at=_START + timedelta(seconds=3),
    )
    resumed = transition_discussion_run(
        paused, DiscussionRunStatus.RUNNING, at=_START + timedelta(seconds=30),
    )
    assert resumed.started_at == running.started_at
    assert resumed.agent_turns_used == 1
    assert resumed.stop_reason is None
    assert reserve_discussion_turn(resumed, at=_START + timedelta(seconds=31)).agent_turns_used == 2
    with pytest.raises(ValueError, match="time limit reached"):
        reserve_discussion_turn(resumed, at=_START + timedelta(seconds=61))

    awaiting = transition_discussion_run(
        resumed, DiscussionRunStatus.AWAITING_HUMAN,
        reason=DiscussionStopReason.HUMAN_INPUT_NEEDED,
        at=_START + timedelta(seconds=32),
    )
    assert awaiting.stopped_at == _START + timedelta(seconds=32)
    with pytest.raises(ValueError, match="invalid discussion transition"):
        transition_discussion_run(awaiting, DiscussionRunStatus.RUNNING)


def test_turn_limit_and_end_reason_are_deterministic() -> None:
    created = run(limits=DiscussionRunLimits(max_agent_turns=2))
    running = transition_discussion_run(
        created, DiscussionRunStatus.RUNNING, at=_START + timedelta(seconds=1),
    )
    first = reserve_discussion_turn(running, at=_START + timedelta(seconds=2))
    second = reserve_discussion_turn(first, at=_START + timedelta(seconds=3))
    with pytest.raises(ValueError, match="Agent turn limit reached"):
        reserve_discussion_turn(second, at=_START + timedelta(seconds=4))
    limited = transition_discussion_run(
        second, DiscussionRunStatus.LIMIT_REACHED,
        reason=DiscussionStopReason.TURN_LIMIT,
        at=_START + timedelta(seconds=4),
    )
    assert limited.agent_turns_used == 2
    assert limited.stop_reason is DiscussionStopReason.TURN_LIMIT
    with pytest.raises(ValueError, match="invalid discussion transition"):
        transition_discussion_run(limited, DiscussionRunStatus.RUNNING)


def test_restart_and_cancelled_batches_never_resume_implicitly() -> None:
    created = run()
    cancelled = transition_discussion_run(
        created, DiscussionRunStatus.CANCELLED,
        reason=DiscussionStopReason.HUMAN_CANCELLED,
        at=_START + timedelta(seconds=1),
    )
    assert cancelled.started_at is None
    assert cancelled.stopped_at == _START + timedelta(seconds=1)
    running = transition_discussion_run(
        created, DiscussionRunStatus.RUNNING, at=_START + timedelta(seconds=1),
    )
    interrupted = transition_discussion_run(
        running, DiscussionRunStatus.INTERRUPTED,
        reason=DiscussionStopReason.SERVER_RESTART,
        at=_START + timedelta(seconds=2),
    )
    with pytest.raises(ValueError, match="invalid discussion transition"):
        transition_discussion_run(interrupted, DiscussionRunStatus.RUNNING)


def test_invalid_transitions_reasons_and_timestamps_fail_closed() -> None:
    created = run()
    with pytest.raises(ValueError, match="invalid discussion transition"):
        transition_discussion_run(created, DiscussionRunStatus.FINISHED)
    with pytest.raises(ValidationError, match="status and stop_reason do not match"):
        transition_discussion_run(
            created, DiscussionRunStatus.CANCELLED,
            reason=DiscussionStopReason.AGENT_FINISHED,
            at=_START + timedelta(seconds=1),
        )
    running = transition_discussion_run(
        created, DiscussionRunStatus.RUNNING, at=_START + timedelta(seconds=1),
    )
    with pytest.raises(ValidationError, match="status and stop_reason do not match"):
        transition_discussion_run(
            running, DiscussionRunStatus.PAUSED,
            reason=DiscussionStopReason.HUMAN_INPUT_NEEDED,
            at=_START + timedelta(seconds=2),
        )
    with pytest.raises(ValidationError, match="turn limit requires"):
        transition_discussion_run(
            running, DiscussionRunStatus.LIMIT_REACHED,
            reason=DiscussionStopReason.TURN_LIMIT,
            at=_START + timedelta(seconds=2),
        )
    with pytest.raises(ValidationError, match="time limit requires"):
        transition_discussion_run(
            running, DiscussionRunStatus.LIMIT_REACHED,
            reason=DiscussionStopReason.TIME_LIMIT,
            at=_START + timedelta(seconds=2),
        )
    timed_out = transition_discussion_run(
        running, DiscussionRunStatus.LIMIT_REACHED,
        reason=DiscussionStopReason.TIME_LIMIT,
        at=_START + timedelta(seconds=601),
    )
    assert timed_out.stop_reason is DiscussionStopReason.TIME_LIMIT
    with pytest.raises(ValidationError, match="started_at is out of order"):
        reserve_discussion_turn(running, at=_START)
    with pytest.raises(ValidationError, match="cannot stop before it starts"):
        run(
            status=DiscussionRunStatus.FINISHED,
            stop_reason=DiscussionStopReason.AGENT_FINISHED,
            started_at=_START + timedelta(seconds=2),
            stopped_at=_START + timedelta(seconds=1),
            updated_at=_START + timedelta(seconds=3),
        )


def test_reply_contract_routes_only_structured_handoff() -> None:
    reply = DiscussionReply(
        content="月见、鲸鲸，你们怎么看？",
        next_action=DiscussionNextAction.HANDOFF,
        handoff_to=(MemberRole.IMPLEMENTER, MemberRole.REVIEWER),
    ).validate_for_speaker(MemberRole.PLANNER)
    assert reply.handoff_to == (MemberRole.IMPLEMENTER, MemberRole.REVIEWER)
    assert DiscussionReply.model_validate_json(reply.model_dump_json()) == reply
    plain_mention = DiscussionReply(
        content="@月见 我先说完，等 Human 再决定。",
        next_action=DiscussionNextAction.AWAIT_HUMAN,
    )
    assert plain_mention.handoff_to == ()

    with pytest.raises(ValidationError, match="requires at least one teammate"):
        DiscussionReply(content="请接话", next_action=DiscussionNextAction.HANDOFF)
    with pytest.raises(ValidationError, match="non-handoff action"):
        DiscussionReply(content="结束", next_action=DiscussionNextAction.FINISH,
                        handoff_to=(MemberRole.REVIEWER,))
    with pytest.raises(ValidationError, match="targets must be unique"):
        DiscussionReply(content="重复", next_action=DiscussionNextAction.HANDOFF,
                        handoff_to=(MemberRole.REVIEWER, MemberRole.REVIEWER))
    with pytest.raises(ValidationError, match="targets must be Agents"):
        DiscussionReply(content="越权", next_action=DiscussionNextAction.HANDOFF,
                        handoff_to=(MemberRole.HUMAN,))
    with pytest.raises(ValueError, match="cannot hand off to itself"):
        reply.validate_for_speaker(MemberRole.IMPLEMENTER)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DiscussionReply(content="越权", next_action=DiscussionNextAction.FINISH,
                        write_paths=("src",))
