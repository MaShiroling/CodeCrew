"""One Draft-07 wire contract for Reviewer prompts, CLI and local validation.

Artifact ownership, actual evidence and prior issue IDs remain stateful checks.
No model output is repaired or converted into a missing review report here.
"""

from copy import deepcopy
from typing import Any

from app.team.actions import MAX_ACTIONS_PER_TURN, AgentChatTurn
from app.team.models import MemberRole, RoomMember
from app.verification import ReviewIssue, ReviewIssuePriority

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
REVIEW_ACTIONS = ("approve_review", "request_rework")
REVIEWER_ACTIONS = (
    "send_message",
    "ask_question",
    "answer_question",
    "share_artifact",
    "report_progress",
    *REVIEW_ACTIONS,
    "request_human_input",
    "finish_turn",
)


def _recipient_schema(roles: tuple[MemberRole, ...], members: tuple[RoomMember, ...]) -> dict:
    variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "role"],
            "properties": {
                "kind": {"const": "role"},
                "role": {"enum": [role.value for role in roles]},
                "member_id": {"type": "null"},
            },
        }
    ]
    ids = [str(member.member_id) for member in members if member.role in roles]
    if ids:
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "member_id"],
                "properties": {
                    "kind": {"const": "member"},
                    "member_id": {"enum": ids},
                    "role": {"type": "null"},
                },
            }
        )
    return {"oneOf": variants}


def _when(action: str | tuple[str, ...], rule: dict) -> dict:
    names = (action,) if isinstance(action, str) else action
    return {
        "if": {"properties": {"action": {"enum": list(names)}}, "required": ["action"]},
        "then": rule,
    }


def reviewer_turn_schema(members: tuple[RoomMember, ...] = ()) -> dict[str, Any]:
    """Fresh schema; bind member recipients to trusted room identities, not prose."""
    schema = deepcopy(AgentChatTurn.model_json_schema())
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["title"] = "CodeCrewReviewerTurn"
    definitions = schema["$defs"]
    issue_schema = ReviewIssue.model_json_schema()
    definitions.update(issue_schema.pop("$defs", {}))
    # ReviewIssue is also an internal model with defaults. Wire reports must
    # supply stable IDs and explicit booleans; never generate them for the Agent.
    issue_schema["required"] = ["issue_id", "priority", "summary", "resolved"]
    issue_schema["properties"]["issue_id"]["pattern"] = UUID_PATTERN
    issue_schema["properties"]["summary"]["pattern"] = r"\S"
    definitions["ReviewerIssue"] = issue_schema
    definitions["InlineReviewReport"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["issues"],
        "properties": {"issues": {"type": "array", "items": {"$ref": "#/$defs/ReviewerIssue"}}},
    }
    action_schema = definitions["AgentChatAction"]
    action_schema["properties"]["action"] = {"enum": list(REVIEWER_ACTIONS)}
    for variant in action_schema["properties"]["content"]["anyOf"]:
        if variant.get("type") == "string":
            variant["pattern"] = r"\S"
    action_schema["properties"]["artifact_ids"].update(
        {
            "uniqueItems": True,
            "items": {"type": "string", "format": "uuid", "pattern": UUID_PATTERN},
        }
    )
    # Reviewers cannot revise plans. Explicit empty/null unused fields are OK.
    action_schema["properties"]["supersedes_artifact_id"] = {"type": "null"}
    action_schema["properties"]["addresses_message_ids"] = {"type": "array", "maxItems": 0}
    empty_sources = {
        "artifact_ids": {"type": "array", "maxItems": 0},
        "artifact_content": {"type": "null"},
    }
    action_schema["allOf"] = [
        _when(
            "finish_turn",
            {
                "properties": {
                    **empty_sources,
                    "recipient": {"type": "null"},
                    "reply_to": {"type": "null"},
                }
            },
        ),
        {
            "if": {
                "properties": {"action": {"not": {"const": "finish_turn"}}},
                "required": ["action"],
            },
            "then": {
                "required": ["recipient", "content"],
                "properties": {
                    "recipient": _recipient_schema(
                        (MemberRole.IMPLEMENTER, MemberRole.ORCHESTRATOR, MemberRole.HUMAN), members
                    ),
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 16_000,
                        "pattern": r"\S",
                    },
                },
            },
        },
        _when(
            "answer_question",
            {
                "required": ["reply_to"],
                "properties": {
                    "reply_to": {"type": "string", "format": "uuid", "pattern": UUID_PATTERN},
                },
            },
        ),
        _when(
            "share_artifact",
            {"required": ["artifact_ids"], "properties": {"artifact_ids": {"minItems": 1}}},
        ),
        _when(
            "request_human_input",
            {"properties": {"recipient": _recipient_schema((MemberRole.HUMAN,), members)}},
        ),
        _when(
            tuple(name for name in REVIEWER_ACTIONS if name not in REVIEW_ACTIONS),
            {
                "properties": {"artifact_content": {"type": "null"}},
            },
        ),
        _when(
            REVIEW_ACTIONS,
            {
                "properties": {
                    "recipient": _recipient_schema((MemberRole.ORCHESTRATOR,), members),
                    "content": {"maxLength": 4000},
                },
                "oneOf": [
                    {
                        "required": ["artifact_content"],
                        "properties": {
                            "artifact_content": {"$ref": "#/$defs/InlineReviewReport"},
                            "artifact_ids": {"type": "array", "maxItems": 0},
                        },
                    },
                    {
                        "required": ["artifact_ids"],
                        "properties": {
                            "artifact_ids": {"type": "array", "minItems": 1, "maxItems": 1},
                            "artifact_content": {"type": "null"},
                        },
                    },
                ],
            },
        ),
        _when(
            "request_rework",
            {
                "properties": {
                    "artifact_content": {
                        "properties": {
                            "issues": {
                                "contains": {
                                    "type": "object",
                                    "required": ["resolved"],
                                    "properties": {"resolved": {"const": False}},
                                }
                            },
                        }
                    }
                }
            },
        ),
        _when(
            "approve_review",
            {
                "properties": {
                    "artifact_content": {
                        "properties": {
                            "issues": {
                                "not": {
                                    "contains": {
                                        "type": "object",
                                        "required": ["priority", "resolved"],
                                        "properties": {
                                            "priority": {
                                                "enum": [
                                                    ReviewIssuePriority.HIGH.value,
                                                    ReviewIssuePriority.CRITICAL.value,
                                                ]
                                            },
                                            "resolved": {"const": False},
                                        },
                                    }
                                }
                            },
                        }
                    }
                }
            },
        ),
    ]
    definitions["ReviewerFinishAction"] = {
        "allOf": [
            {"$ref": "#/$defs/AgentChatAction"},
            {"properties": {"action": {"const": "finish_turn"}}},
        ]
    }
    definitions["ReviewerNonTerminalAction"] = {
        "allOf": [
            {"$ref": "#/$defs/AgentChatAction"},
            {"properties": {"action": {"not": {"const": "finish_turn"}}}},
        ]
    }
    # Draft-07 tuple validation expresses the existing bounded final-action
    # rule without silently removing multi-action Reviewer conversations.
    schema["properties"]["actions"]["oneOf"] = [
        {
            "minItems": count,
            "maxItems": count,
            "items": [{"$ref": "#/$defs/ReviewerNonTerminalAction"}] * (count - 1)
            + [{"$ref": "#/$defs/ReviewerFinishAction"}],
        }
        for count in range(1, MAX_ACTIONS_PER_TURN + 1)
    ]
    return schema
