from dataclasses import dataclass

from app.messaging.mailbox import Mailbox
from app.messaging.models import HandoffEnvelope, HandoffParty, MailboxMessage
from app.storage.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)


class ArtifactReferenceValidationError(RuntimeError):
    """Raised when a handoff points at an unavailable or mismatched artifact."""


@dataclass(frozen=True, slots=True)
class HandoffBatch:
    """Validated deliveries and messages rejected during artifact verification."""

    accepted: tuple[MailboxMessage, ...]
    rejected: tuple[MailboxMessage, ...]


class HandoffService:
    """Integrity boundary between durable artifacts and A2A delivery."""

    def __init__(self, mailbox: Mailbox, artifacts: ArtifactStore) -> None:
        if mailbox.database.path != artifacts.database.path:
            raise ValueError("mailbox and artifact store must use the same database")
        self.mailbox = mailbox
        self.artifacts = artifacts

    def initialize(self) -> None:
        self.artifacts.initialize()
        self.mailbox.initialize()

    def send(self, envelope: HandoffEnvelope) -> MailboxMessage:
        self.validate_artifacts(envelope)
        return self.mailbox.send(envelope)

    def receive(self, recipient: HandoffParty, *, limit: int = 10) -> HandoffBatch:
        accepted: list[MailboxMessage] = []
        rejected: list[MailboxMessage] = []
        for message in self.mailbox.receive(recipient, limit=limit):
            try:
                self.validate_artifacts(message.envelope)
            except ArtifactReferenceValidationError as exc:
                failed = self.mailbox.mark_failed(
                    message.envelope.message_id,
                    reason=f"artifact validation failed: {exc}",
                )
                rejected.append(failed)
            else:
                accepted.append(message)
        return HandoffBatch(accepted=tuple(accepted), rejected=tuple(rejected))

    def validate_artifacts(self, envelope: HandoffEnvelope) -> None:
        seen = set()
        for reference in envelope.artifacts:
            if reference.artifact_id in seen:
                raise ArtifactReferenceValidationError(
                    f"duplicate artifact reference: {reference.artifact_id}"
                )
            seen.add(reference.artifact_id)
            try:
                artifact = self.artifacts.get_metadata(reference.artifact_id)
            except ArtifactNotFoundError as exc:
                raise ArtifactReferenceValidationError(
                    f"artifact does not exist: {reference.artifact_id}"
                ) from exc

            mismatches: list[str] = []
            if artifact.task_id != envelope.task_id:
                mismatches.append("task_id")
            if artifact.trace_id != envelope.trace_id:
                mismatches.append("trace_id")
            if artifact.type is not reference.type:
                mismatches.append("type")
            if artifact.sha256 != reference.sha256:
                mismatches.append("sha256")
            if mismatches:
                fields = ", ".join(mismatches)
                raise ArtifactReferenceValidationError(
                    f"artifact {reference.artifact_id} mismatches envelope fields: {fields}"
                )

            try:
                for _ in self.artifacts.iter_bytes(reference.artifact_id):
                    pass
            except ArtifactIntegrityError as exc:
                raise ArtifactReferenceValidationError(str(exc)) from exc
