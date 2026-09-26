"""Reviewer wire rules apply identically before native and text output routing."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from jsonschema import Draft7Validator, FormatChecker

from app.team.actions import ChatActionError, parse_agent_chat_turn
from app.team.models import MemberKind, MemberRole, RoomMember
from app.team.reviewer_contract import reviewer_turn_schema


def issue(**updates):
    return {
        "issue_id": str(uuid4()),
        "priority": "high",
        "summary": "Evidence-backed defect",
        "resolved": False,
        **updates,
    }


def review(action="request_rework", **updates):
    return {
        "actions": [
            {
                "action": action,
                "recipient": {"kind": "role", "role": "orchestrator"},
                "content": "Evidence-based conclusion",
                "artifact_content": {"issues": [issue()]},
                **updates,
            },
            {"action": "finish_turn"},
        ]
    }


def validate(payload, *, native, schema=None):
    schema = schema or reviewer_turn_schema()
    output = (
        {"structured_output": payload, "result": "not a fallback"}
        if native
        else {
            "result": json.dumps(payload),
        }
    )
    return parse_agent_chat_turn(output, require_structured_output=native, output_schema=schema)


def test_schema_is_valid_and_each_call_is_independent():
    schema = reviewer_turn_schema()
    Draft7Validator.check_schema(schema)
    schema["$defs"]["AgentChatAction"]["properties"]["action"]["enum"].clear()
    assert reviewer_turn_schema()["$defs"]["AgentChatAction"]["properties"]["action"]["enum"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        review(),
        review("approve_review", artifact_content={"issues": []}),
        review("approve_review", artifact_content={"issues": [issue(resolved=True)]}),
        review("approve_review", artifact_content={"issues": [issue(priority="medium")]}),
        review(artifact_content=None, artifact_ids=[str(uuid4())]),
        review(artifact_ids=[]),
        {"actions": [{"action": "finish_turn"}]},
    ],
    ids=["rework", "approve", "resolved", "nonblocking", "reference", "empty-unused", "finish"],
)
def test_valid_reviews_share_native_and_text_contract(native, payload):
    Draft7Validator(reviewer_turn_schema(), format_checker=FormatChecker()).validate(payload)
    assert validate(payload, native=native).actions[-1].action.value == "finish_turn"


def invalid_payloads():
    values = {}

    def add(name, **updates):
        values[name] = review(**updates)

    add("no-source", artifact_content=None)
    add("two-sources", artifact_ids=[str(uuid4())])
    add("two-references", artifact_content=None, artifact_ids=[str(uuid4()), str(uuid4())])
    add("invalid-reference", artifact_content=None, artifact_ids=["bad-id"])
    add("missing-issues", artifact_content={})
    add("report-extra-key", artifact_content={"issues": [issue()], "verdict": "rejected"})
    add("wrong-recipient", recipient={"kind": "role", "role": "implementer"})
    add("room-broadcast", recipient={"kind": "room"})
    add("blank-content", content="  ")
    add("long-content", content="x" * 4001)
    add("no-unresolved", artifact_content={"issues": []})
    add("all-resolved", artifact_content={"issues": [issue(resolved=True)]})
    for field in ("issue_id", "priority", "summary", "resolved"):
        value = issue()
        value.pop(field)
        add(f"missing-{field}", artifact_content={"issues": [value]})
    for name, changes in {
        "bad-uuid": {"issue_id": "bad-id"},
        "bad-priority": {"priority": "urgent"},
        "empty-summary": {"summary": ""},
        "blank-summary": {"summary": "  "},
        "long-summary": {"summary": "x" * 1001},
        "string-resolved": {"resolved": "false"},
        "number-resolved": {"resolved": 0},
        "extra-issue-field": {"evidence": "invented"},
    }.items():
        add(name, artifact_content={"issues": [issue(**changes)]})
    for priority in ("high", "critical"):
        add(
            f"unresolved-{priority}-approval",
            action="approve_review",
            artifact_content={"issues": [issue(priority=priority)]},
        )
    for action in ("share_plan", "request_review"):
        add(f"forbidden-{action}", action=action)
    values["no-finish"] = {"actions": [review()["actions"][0]]}
    values["first-finish"] = {"actions": list(reversed(review()["actions"]))}
    values["multiple-finish"] = {"actions": [{"action": "finish_turn"}] * 2}
    values["too-many-actions"] = {
        "actions": [review()["actions"][0]] * 20 + [{"action": "finish_turn"}]
    }
    values["unknown-top-field"] = {**review(), "unknown": 1}
    values["terminal-recipient"] = deepcopy(review())
    values["terminal-recipient"]["actions"][-1]["recipient"] = {"kind": "role", "role": "human"}
    return values


INVALID = invalid_payloads()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("case", list(INVALID))
def test_invalid_reviews_rejected_by_schema_and_both_local_sources(native, case):
    payload = INVALID[case]
    assert not Draft7Validator(reviewer_turn_schema(), format_checker=FormatChecker()).is_valid(
        payload
    )
    with pytest.raises(ChatActionError, match="Reviewer output contract"):
        validate(payload, native=native)


@pytest.mark.parametrize("native", [False, True])
def test_duplicate_issue_identity_is_rejected_without_repair(native):
    first = issue(issue_id="abcdef01-0000-4000-8000-000000000001")
    payload = review(
        artifact_content={
            "issues": [first, {**first, "issue_id": first["issue_id"].upper(), "resolved": True}]
        }
    )
    original = deepcopy(payload)
    with pytest.raises(ChatActionError, match="duplicate issue IDs"):
        validate(payload, native=native)
    assert payload == original


def test_recipient_member_is_bound_to_trusted_room_role():
    room_id = uuid4()
    members = tuple(
        RoomMember(room_id=room_id, name=role.value, role=role, kind=kind)
        for role, kind in (
            (MemberRole.ORCHESTRATOR, MemberKind.SYSTEM),
            (MemberRole.IMPLEMENTER, MemberKind.AGENT),
        )
    )
    schema = reviewer_turn_schema(members)
    payload = review(recipient={"kind": "member", "member_id": str(members[0].member_id)})
    validate(payload, native=True, schema=schema)
    for member_id in (members[1].member_id, uuid4()):
        payload["actions"][0]["recipient"]["member_id"] = str(member_id)
        with pytest.raises(ChatActionError, match="Reviewer output contract"):
            validate(payload, native=True, schema=schema)


def test_multi_action_reviewer_chat_is_preserved():
    payload = {
        "actions": [
            {
                "action": "ask_question",
                "recipient": {"kind": "role", "role": "implementer"},
                "content": "Explain this diff",
            },
            {
                "action": "request_human_input",
                "recipient": {"kind": "role", "role": "human"},
                "content": "Clarify acceptance",
            },
            {
                "action": "answer_question",
                "recipient": {"kind": "role", "role": "orchestrator"},
                "reply_to": str(uuid4()),
                "content": "Evidence inspected",
            },
            {
                "action": "share_artifact",
                "recipient": {"kind": "role", "role": "implementer"},
                "artifact_ids": [str(uuid4())],
                "content": "Review evidence",
            },
            {"action": "finish_turn"},
        ]
    }
    assert len(validate(payload, native=False).actions) == 5
