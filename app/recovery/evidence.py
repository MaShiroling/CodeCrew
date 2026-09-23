import json
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.storage.artifacts import ArtifactStore, ArtifactStoreError
from app.storage.models import ArtifactMetadata, ArtifactReference, ArtifactType
from app.trace.models import StoredTraceEvent, TraceEventType
from app.trace.store import TraceStore
from app.verification.completion import (
    CompletionCondition,
    CompletionDecision,
    ReviewIssue,
    ReviewReport,
    ReviewVerdict,
)
from app.verification.verifier import (
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)
from app.workspace.changes import ChangedFile, WorkspaceChangeSet
from app.workspace.commands import CommandResult
from app.workspace.permissions import (
    PermissionPolicy,
    PermissionReport,
    PermissionViolation,
)


class EvidenceRecoveryError(RuntimeError):
    """Raised when persisted workflow evidence is missing or inconsistent."""


class _TaskIdentity(Protocol):
    id: UUID
    trace_id: UUID


class _RuntimeEvidenceTarget(Protocol):
    task: _TaskIdentity
    latest_verification: VerificationReport | None
    latest_completion: CompletionDecision | None


class RecoveredEvidence(BaseModel):
    """Latest trustworthy evidence reconstructed for one task trace."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    verification: VerificationReport | None = None
    review: ReviewReport | None = None
    completion: CompletionDecision | None = None
    verification_sequence: int | None = None
    review_sequence: int | None = None
    completion_sequence: int | None = None


class EvidenceRecoveryService:
    """Rebuilds runtime evidence from immutable artifacts and the trace index."""

    def __init__(self, artifacts: ArtifactStore, traces: TraceStore) -> None:
        if artifacts.database.path != traces.database.path:
            raise ValueError("artifact and trace stores must share one database")
        self.artifacts = artifacts
        self.traces = traces

    def recover(self, *, task_id: UUID, trace_id: UUID) -> RecoveredEvidence:
        latest = self._latest_evidence_events(task_id=task_id, trace_id=trace_id)
        verification_event = latest.get(TraceEventType.VERIFICATION_COMPLETED)
        review_event = latest.get(TraceEventType.REVIEW_DECIDED)
        completion_event = latest.get(TraceEventType.COMPLETION_DECIDED)

        verification = (
            self.load_verification(
                self._event_artifact_id(verification_event),
                task_id=task_id,
                trace_id=trace_id,
            )
            if verification_event is not None
            else None
        )
        review = (
            self.load_review(
                self._event_artifact_id(review_event),
                task_id=task_id,
                trace_id=trace_id,
            )
            if review_event is not None
            else None
        )

        completion: CompletionDecision | None = None
        completion_sequence: int | None = None
        if completion_event is not None:
            newest_input_sequence = (
                max(
                    event.sequence
                    for event in (verification_event, review_event)
                    if event is not None
                )
                if verification_event is not None or review_event is not None
                else 0
            )
            if completion_event.sequence > newest_input_sequence:
                completion, verification_id, review_id = self._load_completion(
                    self._event_artifact_id(completion_event),
                    task_id=task_id,
                    trace_id=trace_id,
                )
                if verification is None or review is None:
                    raise EvidenceRecoveryError(
                        "completion decision has no recoverable verification and review inputs"
                    )
                if verification_id != verification.artifact.artifact_id:
                    raise EvidenceRecoveryError(
                        "completion decision references a different verification report"
                    )
                if review_id != review.artifact.artifact_id:
                    raise EvidenceRecoveryError(
                        "completion decision references a different review report"
                    )
                completion_sequence = completion_event.sequence

        return RecoveredEvidence(
            verification=verification,
            review=review,
            completion=completion,
            verification_sequence=(
                verification_event.sequence if verification_event is not None else None
            ),
            review_sequence=review_event.sequence if review_event is not None else None,
            completion_sequence=completion_sequence,
        )

    def restore_runtime(self, runtime: _RuntimeEvidenceTarget) -> RecoveredEvidence:
        """Restore evidence fields on an already reconstructed WorkflowRuntime."""
        recovered = self.recover(
            task_id=runtime.task.id,
            trace_id=runtime.task.trace_id,
        )
        runtime.latest_verification = recovered.verification
        runtime.latest_completion = recovered.completion
        return recovered

    def load_verification(
        self,
        artifact_id: UUID,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> VerificationReport:
        try:
            metadata, reference, content = self._load_json(
                artifact_id,
                ArtifactType.VERIFICATION_REPORT,
                task_id=task_id,
                trace_id=trace_id,
            )
            self._validate_identity(content, task_id=task_id, trace_id=trace_id)
            checks = tuple(VerificationCheck.model_validate(item) for item in content["checks"])
            passed = self._require_bool(content, "passed")
            if passed != all(check.status is VerificationStatus.PASSED for check in checks):
                raise EvidenceRecoveryError("verification passed flag disagrees with its checks")
            for check in checks:
                for evidence in check.evidence:
                    self._validate_embedded_reference(evidence, task_id=task_id, trace_id=trace_id)

            change_set = self._load_change_set(
                self._uuid(content, "change_set_artifact_id"),
                task_id=task_id,
                trace_id=trace_id,
            )
            expected_diff = self._optional_uuid(content, "diff_artifact_id")
            actual_diff = (
                change_set.diff_artifact.artifact_id
                if change_set.diff_artifact is not None
                else None
            )
            if expected_diff != actual_diff:
                raise EvidenceRecoveryError("verification and change-set diff references disagree")
            permission = self._load_permission_report(
                self._uuid(content, "permission_artifact_id"),
                task_id=task_id,
                trace_id=trace_id,
            )
            if permission.task_id != change_set.task_id:
                raise EvidenceRecoveryError("permission evidence crosses task boundaries")
            command_results = tuple(
                self._load_command_result(UUID(value), task_id=task_id, trace_id=trace_id)
                for value in content.get("command_audit_artifact_ids", [])
            )
            return VerificationReport(
                task_id=task_id,
                trace_id=trace_id,
                passed=passed,
                checks=checks,
                change_set=change_set,
                permission_report=permission,
                command_results=command_results,
                artifact=reference,
                verified_at=metadata.created_at,
            )
        except EvidenceRecoveryError:
            raise
        except (
            ArtifactStoreError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise EvidenceRecoveryError(
                f"invalid verification artifact {artifact_id}: {exc}"
            ) from exc

    def load_review(
        self,
        artifact_id: UUID,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> ReviewReport:
        try:
            metadata, reference, content = self._load_json(
                artifact_id,
                ArtifactType.REVIEW_REPORT,
                task_id=task_id,
                trace_id=trace_id,
            )
            self._validate_identity(content, task_id=task_id, trace_id=trace_id)
            issues = tuple(ReviewIssue.model_validate(item) for item in content["issues"])
            return ReviewReport(
                task_id=task_id,
                trace_id=trace_id,
                reviewer=content["reviewer"],
                verdict=ReviewVerdict(content["verdict"]),
                issues=issues,
                summary=content["summary"],
                artifact=reference,
                reviewed_at=metadata.created_at,
            )
        except EvidenceRecoveryError:
            raise
        except (
            ArtifactStoreError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise EvidenceRecoveryError(f"invalid review artifact {artifact_id}: {exc}") from exc

    def load_completion(
        self,
        artifact_id: UUID,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> CompletionDecision:
        decision, _, _ = self._load_completion(artifact_id, task_id=task_id, trace_id=trace_id)
        return decision

    def _load_completion(
        self,
        artifact_id: UUID,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> tuple[CompletionDecision, UUID, UUID]:
        try:
            metadata, reference, content = self._load_json(
                artifact_id,
                ArtifactType.COMPLETION_DECISION,
                task_id=task_id,
                trace_id=trace_id,
            )
            self._validate_identity(content, task_id=task_id, trace_id=trace_id)
            conditions = tuple(
                CompletionCondition.model_validate(item) for item in content["conditions"]
            )
            for condition in conditions:
                for evidence in condition.evidence:
                    self._validate_embedded_reference(evidence, task_id=task_id, trace_id=trace_id)
            decision = CompletionDecision(
                task_id=task_id,
                trace_id=trace_id,
                passed=self._require_bool(content, "passed"),
                conditions=conditions,
                failed_conditions=tuple(content["failed_conditions"]),
                artifact=reference,
                decided_at=metadata.created_at,
            )
            return (
                decision,
                self._uuid(content, "verification_artifact_id"),
                self._uuid(content, "review_artifact_id"),
            )
        except EvidenceRecoveryError:
            raise
        except (
            ArtifactStoreError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise EvidenceRecoveryError(
                f"invalid completion artifact {artifact_id}: {exc}"
            ) from exc

    def _load_change_set(
        self, artifact_id: UUID, *, task_id: UUID, trace_id: UUID
    ) -> WorkspaceChangeSet:
        metadata, reference, content = self._load_json(
            artifact_id,
            ArtifactType.CHANGESET,
            task_id=task_id,
            trace_id=trace_id,
        )
        self._validate_identity(content, task_id=task_id, trace_id=trace_id)
        diff_id = self._optional_uuid(content, "diff_artifact_id")
        diff = (
            self._reference(
                diff_id,
                ArtifactType.DIFF,
                task_id=task_id,
                trace_id=trace_id,
            )
            if diff_id is not None
            else None
        )
        return WorkspaceChangeSet(
            task_id=task_id,
            trace_id=trace_id,
            base_revision=content["base_revision"],
            head_revision=content["head_revision"],
            changed_files=tuple(
                ChangedFile.model_validate(item) for item in content["changed_files"]
            ),
            has_effective_diff=self._require_bool(content, "has_effective_diff"),
            diff_artifact=diff,
            manifest_artifact=reference,
            collected_at=metadata.created_at,
        )

    def _load_permission_report(
        self, artifact_id: UUID, *, task_id: UUID, trace_id: UUID
    ) -> PermissionReport:
        metadata, reference, content = self._load_json(
            artifact_id,
            ArtifactType.PERMISSION_REPORT,
            task_id=task_id,
            trace_id=trace_id,
        )
        self._validate_identity(content, task_id=task_id, trace_id=trace_id)
        violations = tuple(
            PermissionViolation.model_validate(item) for item in content["violations"]
        )
        passed = self._require_bool(content, "passed")
        if passed != (not violations):
            raise EvidenceRecoveryError("permission passed flag disagrees with recorded violations")
        return PermissionReport(
            task_id=task_id,
            trace_id=trace_id,
            policy=PermissionPolicy.model_validate(content["policy"]),
            checked_paths=tuple(content["checked_paths"]),
            violations=violations,
            passed=passed,
            artifact=reference,
            checked_at=metadata.created_at,
        )

    def _load_command_result(
        self, artifact_id: UUID, *, task_id: UUID, trace_id: UUID
    ) -> CommandResult:
        _, reference, content = self._load_json(
            artifact_id,
            ArtifactType.COMMAND_AUDIT,
            task_id=task_id,
            trace_id=trace_id,
        )
        self._validate_identity(content, task_id=task_id, trace_id=trace_id)
        stdout_id = self._optional_uuid(content, "stdout_artifact_id")
        stderr_id = self._optional_uuid(content, "stderr_artifact_id")
        return CommandResult(
            command_id=content["command_id"],
            task_id=task_id,
            trace_id=trace_id,
            argv=tuple(content["argv"]),
            working_directory=content["working_directory"],
            status=content["status"],
            matched_rule=content.get("matched_rule"),
            exit_code=content.get("exit_code"),
            duration_ms=content["duration_ms"],
            denial_reason=content.get("denial_reason"),
            stdout_artifact=(
                self._reference(
                    stdout_id,
                    ArtifactType.TEST_LOG,
                    task_id=task_id,
                    trace_id=trace_id,
                )
                if stdout_id is not None
                else None
            ),
            stderr_artifact=(
                self._reference(
                    stderr_id,
                    ArtifactType.TEST_LOG,
                    task_id=task_id,
                    trace_id=trace_id,
                )
                if stderr_id is not None
                else None
            ),
            stdout_truncated=content["stdout_truncated"],
            stderr_truncated=content["stderr_truncated"],
            audit_artifact=reference,
            started_at=content["started_at"],
            finished_at=content["finished_at"],
        )

    def _latest_evidence_events(
        self, *, task_id: UUID, trace_id: UUID
    ) -> dict[TraceEventType, StoredTraceEvent]:
        wanted = {
            TraceEventType.VERIFICATION_COMPLETED,
            TraceEventType.REVIEW_DECIDED,
            TraceEventType.COMPLETION_DECIDED,
        }
        latest: dict[TraceEventType, StoredTraceEvent] = {}
        cursor = 0
        while True:
            page = self.traces.list(
                task_id=task_id,
                trace_id=trace_id,
                after_sequence=cursor,
                limit=500,
            )
            if not page:
                return latest
            for stored in page:
                if stored.event.type in wanted:
                    latest[stored.event.type] = stored
            cursor = page[-1].sequence

    def _event_artifact_id(self, stored: StoredTraceEvent) -> UUID:
        payload = stored.event.payload
        value = payload.get("artifact_id")
        if value is None and stored.event.type is TraceEventType.REVIEW_DECIDED:
            values = payload.get("artifact_ids")
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, str):
                        continue
                    try:
                        candidate = UUID(item)
                        if (
                            self.artifacts.get_metadata(candidate).type
                            is ArtifactType.REVIEW_REPORT
                        ):
                            value = item
                            break
                    except (ArtifactStoreError, ValueError):
                        continue
        if not isinstance(value, str):
            raise EvidenceRecoveryError(
                f"trace event {stored.event.event_id} has no evidence artifact"
            )
        try:
            return UUID(value)
        except ValueError as exc:
            raise EvidenceRecoveryError(
                f"trace event {stored.event.event_id} has an invalid artifact ID"
            ) from exc

    def _load_json(
        self,
        artifact_id: UUID,
        expected_type: ArtifactType,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> tuple[ArtifactMetadata, ArtifactReference, dict[str, Any]]:
        metadata = self.artifacts.get_metadata(artifact_id)
        self._validate_metadata(
            metadata,
            expected_type,
            task_id=task_id,
            trace_id=trace_id,
        )
        raw = b"".join(self.artifacts.iter_bytes(artifact_id))
        content = json.loads(raw.decode("utf-8"))
        if not isinstance(content, dict):
            raise EvidenceRecoveryError(f"artifact {artifact_id} must contain a JSON object")
        return (
            metadata,
            ArtifactReference.from_metadata(
                metadata, summary=metadata.filename or expected_type.value
            ),
            content,
        )

    def _reference(
        self,
        artifact_id: UUID,
        expected_type: ArtifactType,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> ArtifactReference:
        metadata = self.artifacts.get_metadata(artifact_id)
        self._validate_metadata(
            metadata,
            expected_type,
            task_id=task_id,
            trace_id=trace_id,
        )
        # Consume the iterator so the content hash and size are verified.
        for _ in self.artifacts.iter_bytes(artifact_id):
            pass
        return ArtifactReference.from_metadata(
            metadata, summary=metadata.filename or expected_type.value
        )

    def _validate_embedded_reference(
        self, reference: ArtifactReference, *, task_id: UUID, trace_id: UUID
    ) -> None:
        actual = self._reference(
            reference.artifact_id,
            reference.type,
            task_id=task_id,
            trace_id=trace_id,
        )
        if actual.sha256 != reference.sha256:
            raise EvidenceRecoveryError(
                f"artifact reference hash mismatch: {reference.artifact_id}"
            )

    @staticmethod
    def _validate_metadata(
        metadata: ArtifactMetadata,
        expected_type: ArtifactType,
        *,
        task_id: UUID,
        trace_id: UUID,
    ) -> None:
        if metadata.task_id != task_id or metadata.trace_id != trace_id:
            raise EvidenceRecoveryError(
                f"artifact {metadata.artifact_id} crosses task or trace boundaries"
            )
        if metadata.type is not expected_type:
            raise EvidenceRecoveryError(
                f"artifact {metadata.artifact_id} has type {metadata.type.value}, "
                f"expected {expected_type.value}"
            )

    @staticmethod
    def _validate_identity(content: dict[str, Any], *, task_id: UUID, trace_id: UUID) -> None:
        if content.get("task_id") != str(task_id):
            raise EvidenceRecoveryError("artifact payload has a different task ID")
        if content.get("trace_id") != str(trace_id):
            raise EvidenceRecoveryError("artifact payload has a different trace ID")

    @staticmethod
    def _uuid(content: dict[str, Any], key: str) -> UUID:
        value = content[key]
        if not isinstance(value, str):
            raise EvidenceRecoveryError(f"{key} must be a UUID string")
        return UUID(value)

    @classmethod
    def _optional_uuid(cls, content: dict[str, Any], key: str) -> UUID | None:
        value = content.get(key)
        if value is None:
            return None
        return cls._uuid(content, key)

    @staticmethod
    def _require_bool(content: dict[str, Any], key: str) -> bool:
        value = content[key]
        if not isinstance(value, bool):
            raise EvidenceRecoveryError(f"{key} must be a boolean")
        return value
