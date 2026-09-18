from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.messaging import (
    HandoffEnvelope,
    HandoffParty,
    HandoffType,
    IdempotencyConflictError,
    InvalidAcknowledgementError,
    Mailbox,
    MailboxError,
    MailboxMessageNotFoundError,
    MailboxMessageStatus,
)
from app.storage import ArtifactStore, SQLiteDatabase


def make_mailbox(tmp_path: Path) -> Mailbox:
    mailbox = Mailbox(SQLiteDatabase(tmp_path / "codecrew.sqlite3"))
    mailbox.initialize()
    return mailbox


def make_envelope(**updates: object) -> HandoffEnvelope:
    values: dict[str, object] = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "correlation_id": uuid4(),
        "idempotency_key": f"plan:{uuid4()}",
        "sender": HandoffParty.PLANNER,
        "recipient": HandoffParty.IMPLEMENTER,
        "type": HandoffType.PLAN_READY,
        "payload": {"attempt": 1},
    }
    values.update(updates)
    return HandoffEnvelope(**values)


def test_send_persists_pending_message_across_restart(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    envelope = make_envelope()

    sent = mailbox.send(envelope)
    reopened = Mailbox(SQLiteDatabase(mailbox.database.path))
    reopened.initialize()

    assert sent.envelope == envelope
    assert sent.status is MailboxMessageStatus.PENDING
    assert reopened.get(envelope.message_id) == sent


def test_mailbox_and_artifact_store_share_migration_database(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    ArtifactStore(database, tmp_path / "artifacts").initialize()
    mailbox = Mailbox(database)

    mailbox.initialize()
    sent = mailbox.send(make_envelope())

    assert mailbox.get(sent.envelope.message_id) == sent
    assert database.schema_version == 2


def test_send_is_idempotent_for_same_logical_message(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    envelope = make_envelope()

    first = mailbox.send(envelope)
    second = mailbox.send(envelope.model_copy(update={"message_id": uuid4()}))

    assert second == first
    assert len(mailbox.list_messages()) == 1


def test_idempotency_key_rejects_different_content(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    envelope = make_envelope()
    mailbox.send(envelope)

    with pytest.raises(IdempotencyConflictError, match="different message content"):
        mailbox.send(envelope.model_copy(update={"payload": {"attempt": 2}}))


def test_receive_claims_fifo_messages_for_only_one_recipient(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    task_id = uuid4()
    first = mailbox.send(make_envelope(task_id=task_id))
    second = mailbox.send(make_envelope(task_id=task_id))
    mailbox.send(make_envelope(recipient=HandoffParty.REVIEWER))

    received = mailbox.receive(HandoffParty.IMPLEMENTER, limit=1)

    assert [item.envelope.message_id for item in received] == [first.envelope.message_id]
    assert received[0].status is MailboxMessageStatus.DELIVERED
    assert received[0].delivered_at is not None
    assert mailbox.receive(HandoffParty.IMPLEMENTER)[0].envelope.message_id == second.envelope.message_id
    assert mailbox.receive(HandoffParty.IMPLEMENTER) == ()


def test_acknowledgement_is_recipient_owned_and_idempotent(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    sent = mailbox.send(make_envelope())

    with pytest.raises(InvalidAcknowledgementError, match="must be delivered"):
        mailbox.acknowledge(sent.envelope.message_id, recipient=HandoffParty.IMPLEMENTER)
    mailbox.receive(HandoffParty.IMPLEMENTER)
    with pytest.raises(InvalidAcknowledgementError, match="only the message recipient"):
        mailbox.acknowledge(sent.envelope.message_id, recipient=HandoffParty.REVIEWER)

    acknowledged = mailbox.acknowledge(
        sent.envelope.message_id, recipient=HandoffParty.IMPLEMENTER
    )
    repeated = mailbox.acknowledge(
        sent.envelope.message_id, recipient=HandoffParty.IMPLEMENTER
    )
    assert acknowledged.status is MailboxMessageStatus.ACKNOWLEDGED
    assert acknowledged.acknowledged_at is not None
    assert repeated == acknowledged


def test_failed_message_records_reason_and_cannot_be_acknowledged(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    sent = mailbox.send(make_envelope())

    failed = mailbox.mark_failed(sent.envelope.message_id, reason="adapter unavailable")

    assert failed.status is MailboxMessageStatus.FAILED
    assert failed.failure_reason == "adapter unavailable"
    assert failed.failed_at is not None
    assert mailbox.receive(HandoffParty.IMPLEMENTER) == ()
    with pytest.raises(InvalidAcknowledgementError, match="failed message"):
        mailbox.acknowledge(sent.envelope.message_id, recipient=HandoffParty.IMPLEMENTER)


def test_acknowledged_message_cannot_be_failed(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    sent = mailbox.send(make_envelope())
    mailbox.receive(HandoffParty.IMPLEMENTER)
    mailbox.acknowledge(sent.envelope.message_id, recipient=HandoffParty.IMPLEMENTER)

    with pytest.raises(MailboxError, match="cannot be marked failed"):
        mailbox.mark_failed(sent.envelope.message_id, reason="too late")


def test_filters_by_task_status_recipient_and_correlation(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    task_id = uuid4()
    correlation_id = uuid4()
    expected = mailbox.send(make_envelope(task_id=task_id, correlation_id=correlation_id))
    mailbox.send(make_envelope(task_id=task_id, recipient=HandoffParty.REVIEWER))
    mailbox.receive(HandoffParty.IMPLEMENTER)

    found = mailbox.list_messages(
        task_id=task_id,
        recipient=HandoffParty.IMPLEMENTER,
        status=MailboxMessageStatus.DELIVERED,
        correlation_id=correlation_id,
    )

    assert found == (mailbox.get(expected.envelope.message_id),)


def test_unknown_message_and_invalid_arguments_are_rejected(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)

    with pytest.raises(MailboxMessageNotFoundError, match="not found"):
        mailbox.get(UUID(int=0))
    with pytest.raises(ValueError, match="positive"):
        mailbox.receive(HandoffParty.IMPLEMENTER, limit=0)
    with pytest.raises(ValueError, match="must not be empty"):
        mailbox.mark_failed(UUID(int=0), reason=" ")
