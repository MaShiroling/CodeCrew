"""A2A handoff and mailbox contracts."""

from app.messaging.models import (
    HANDOFF_PROTOCOL_VERSION,
    MAX_HANDOFF_ARTIFACTS,
    MAX_HANDOFF_PAYLOAD_BYTES,
    HandoffEnvelope,
    HandoffParty,
    HandoffType,
    MailboxMessageStatus,
)

__all__ = [
    "HANDOFF_PROTOCOL_VERSION",
    "MAX_HANDOFF_ARTIFACTS",
    "MAX_HANDOFF_PAYLOAD_BYTES",
    "HandoffEnvelope",
    "HandoffParty",
    "HandoffType",
    "MailboxMessageStatus",
]

