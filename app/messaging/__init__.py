"""A2A handoff and mailbox contracts."""

from app.messaging.mailbox import (
    MAILBOX_MIGRATIONS,
    IdempotencyConflictError,
    InvalidAcknowledgementError,
    Mailbox,
    MailboxError,
    MailboxMessageNotFoundError,
)
from app.messaging.models import (
    HANDOFF_PROTOCOL_VERSION,
    MAX_HANDOFF_ARTIFACTS,
    MAX_HANDOFF_PAYLOAD_BYTES,
    HandoffEnvelope,
    HandoffParty,
    HandoffType,
    MailboxMessage,
    MailboxMessageStatus,
)

__all__ = [
    "HANDOFF_PROTOCOL_VERSION",
    "MAILBOX_MIGRATIONS",
    "MAX_HANDOFF_ARTIFACTS",
    "MAX_HANDOFF_PAYLOAD_BYTES",
    "HandoffEnvelope",
    "HandoffParty",
    "HandoffType",
    "IdempotencyConflictError",
    "InvalidAcknowledgementError",
    "Mailbox",
    "MailboxError",
    "MailboxMessage",
    "MailboxMessageNotFoundError",
    "MailboxMessageStatus",
]
