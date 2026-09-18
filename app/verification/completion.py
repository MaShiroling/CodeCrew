from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import utc_now
from app.storage import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStore,
    ArtifactType,
)
from app.verification.verifier import (
    VerificationCheckKind,
    VerificationReport,
    VerificationStatus,
)
from app.workspace import CommandStatus


class CompletionGuardError(RuntimeError):
    """Raised when completion inputs cross task or trace boundaries."""


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewIssuePriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: UUID = Field(default_factory=uuid4)
    priority: ReviewIssuePriority
    summary: str = Field(min_length=1, max_length=1000)
    resolved: bool = False


class ReviewReport(BaseModel):
    """Structured output expected from an independent read-only reviewer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    reviewer: str = Field(min_length=1, max_length=100)
    verdict: ReviewVerdict
    issues: tuple[ReviewIssue, ...] = ()
    summary: str = Field(min_length=1, max_length=4000)
    artifact: ArtifactReference
    reviewed_at: AwareDatetime = Field(default_factory=utc_now)


class CompletionConditionKind(str, Enum):
    VALID_DIFF = "valid_diff"
    STATIC_OR_BUILD = "static_or_build"
    PUBLIC_TESTS = "public_tests"
    HIDDEN_TESTS = "hidden_tests"
    PERMISSIONS = "permissions"
    COMMAND_POLICY = "command_policy"
    VERIFICATION = "verification"
    REVIEW_APPROVAL = "review_approval"
    HIGH_PRIORITY_ISSUES = "high_priority_issues"
    EVIDENCE_INTEGRITY = "evidence_integrity"


class CompletionCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CompletionConditionKind
    passed: bool
    detail: str = Field(min_length=1, max_length=2000)
    evidence: tuple[ArtifactReference, ...] = ()


class CompletionDecision(BaseModel):
    """Final machine decision; only a passing instance may complete a task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    passed: bool
    conditions: tuple[CompletionCondition, ...]
    failed_conditions: tuple[CompletionConditionKind, ...]
    artifact: ArtifactReference
    decided_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_decision(self) -> "CompletionDecision":
        expected_failed = tuple(condition.kind for condition in self.conditions if not condition.passed)
        if self.failed_conditions != expected_failed:
            raise ValueError("failed_conditions must match failed completion conditions")
        if self.passed != (not self.failed_conditions):
            raise ValueError("passed must be derived from completion conditions")
        return self


class CompletionGuard:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def evaluate(
        self,
        verification: VerificationReport,
        review: ReviewReport,
    ) -> CompletionDecision:
        if verification.task_id != review.task_id:
            raise CompletionGuardError("verification and review belong to different tasks")
        if verification.trace_id != review.trace_id:
            raise CompletionGuardError("verification and review belong to different traces")

        task_id = verification.task_id
        trace_id = verification.trace_id
        diff_reference = verification.change_set.diff_artifact
        conditions = [
            CompletionCondition(
                kind=CompletionConditionKind.VALID_DIFF,
                passed=(
                    verification.change_set.has_effective_diff
                    and diff_reference is not None
                    and self._checks_pass(verification, {VerificationCheckKind.DIFF})
                ),
                detail=(
                    f"valid patch covers {len(verification.change_set.changed_files)} files"
                    if verification.change_set.has_effective_diff and diff_reference is not None
                    else "an effective Git patch is required"
                ),
                evidence=tuple(
                    reference
                    for reference in (verification.change_set.manifest_artifact, diff_reference)
                    if reference is not None
                ),
            ),
            self._category_condition(
                verification,
                CompletionConditionKind.STATIC_OR_BUILD,
                {VerificationCheckKind.STATIC_ANALYSIS, VerificationCheckKind.BUILD},
                "static analysis or build checks",
            ),
            self._category_condition(
                verification,
                CompletionConditionKind.PUBLIC_TESTS,
                {VerificationCheckKind.PUBLIC_TESTS},
                "public tests",
            ),
            self._category_condition(
                verification,
                CompletionConditionKind.HIDDEN_TESTS,
                {VerificationCheckKind.HIDDEN_TESTS},
                "hidden tests",
            ),
            CompletionCondition(
                kind=CompletionConditionKind.PERMISSIONS,
                passed=(
                    verification.permission_report.passed
                    and self._checks_pass(verification, {VerificationCheckKind.PERMISSION})
                ),
                detail=(
                    "workspace permission checks passed"
                    if verification.permission_report.passed
                    else "workspace permission violations remain"
                ),
                evidence=(verification.permission_report.artifact,),
            ),
            CompletionCondition(
                kind=CompletionConditionKind.COMMAND_POLICY,
                passed=(
                    self._checks_pass(verification, {VerificationCheckKind.COMMAND_POLICY})
                    and not any(
                        result.status is CommandStatus.DENIED
                        for result in verification.command_results
                    )
                ),
                detail=(
                    "no forbidden command attempts were recorded"
                    if self._checks_pass(
                        verification, {VerificationCheckKind.COMMAND_POLICY}
                    )
                    else "forbidden command attempts were recorded"
                ),
                evidence=tuple(
                    check_evidence
                    for check in verification.checks
                    if check.kind is VerificationCheckKind.COMMAND_POLICY
                    for check_evidence in check.evidence
                ),
            ),
            CompletionCondition(
                kind=CompletionConditionKind.VERIFICATION,
                passed=(
                    verification.passed
                    and all(
                        check.status is VerificationStatus.PASSED
                        for check in verification.checks
                    )
                ),
                detail=(
                    "all deterministic verification checks passed"
                    if verification.passed
                    else "deterministic verification did not pass"
                ),
                evidence=(verification.artifact,),
            ),
            CompletionCondition(
                kind=CompletionConditionKind.REVIEW_APPROVAL,
                passed=review.verdict is ReviewVerdict.APPROVED,
                detail=(
                    f"reviewer {review.reviewer} explicitly approved"
                    if review.verdict is ReviewVerdict.APPROVED
                    else f"reviewer {review.reviewer} rejected the implementation"
                ),
                evidence=(review.artifact,),
            ),
        ]
        unresolved = tuple(
            issue
            for issue in review.issues
            if not issue.resolved
            and issue.priority in {ReviewIssuePriority.HIGH, ReviewIssuePriority.CRITICAL}
        )
        conditions.append(
            CompletionCondition(
                kind=CompletionConditionKind.HIGH_PRIORITY_ISSUES,
                passed=not unresolved,
                detail=(
                    "no unresolved high-priority review issues"
                    if not unresolved
                    else f"{len(unresolved)} high-priority review issues remain unresolved"
                ),
                evidence=(review.artifact,),
            )
        )

        evidence_ok, evidence_detail = self._validate_evidence(
            task_id, trace_id, verification, review
        )
        conditions.append(
            CompletionCondition(
                kind=CompletionConditionKind.EVIDENCE_INTEGRITY,
                passed=evidence_ok,
                detail=evidence_detail,
                evidence=(verification.artifact, review.artifact),
            )
        )
        conditions_tuple = tuple(conditions)
        failed = tuple(condition.kind for condition in conditions_tuple if not condition.passed)
        passed = not failed
        content = {
            "task_id": str(task_id),
            "trace_id": str(trace_id),
            "passed": passed,
            "failed_conditions": [item.value for item in failed],
            "conditions": [item.model_dump(mode="json") for item in conditions_tuple],
            "verification_artifact_id": str(verification.artifact.artifact_id),
            "review_artifact_id": str(review.artifact.artifact_id),
        }
        metadata = self.artifacts.put_json(
            content,
            task_id=task_id,
            trace_id=trace_id,
            type=ArtifactType.COMPLETION_DECISION,
            created_by="completion_guard",
            filename="completion-decision.json",
            metadata={"passed": str(passed).lower()},
        )
        artifact = ArtifactReference.from_metadata(
            metadata,
            summary="Completion guard passed" if passed else "Completion guard rejected task",
        )
        return CompletionDecision(
            task_id=task_id,
            trace_id=trace_id,
            passed=passed,
            conditions=conditions_tuple,
            failed_conditions=failed,
            artifact=artifact,
        )

    @staticmethod
    def _checks_pass(
        verification: VerificationReport, kinds: set[VerificationCheckKind]
    ) -> bool:
        matching = [check for check in verification.checks if check.kind in kinds]
        return bool(matching) and all(
            check.status is VerificationStatus.PASSED for check in matching
        )

    def _category_condition(
        self,
        verification: VerificationReport,
        condition_kind: CompletionConditionKind,
        check_kinds: set[VerificationCheckKind],
        label: str,
    ) -> CompletionCondition:
        matching = tuple(check for check in verification.checks if check.kind in check_kinds)
        passed = bool(matching) and all(
            check.status is VerificationStatus.PASSED for check in matching
        )
        return CompletionCondition(
            kind=condition_kind,
            passed=passed,
            detail=f"{label} passed" if passed else f"{label} are missing or failed",
            evidence=tuple(reference for check in matching for reference in check.evidence),
        )

    def _validate_evidence(
        self,
        task_id: UUID,
        trace_id: UUID,
        verification: VerificationReport,
        review: ReviewReport,
    ) -> tuple[bool, str]:
        references = self._evidence_references(verification, review)
        seen: set[UUID] = set()
        try:
            for reference in references:
                if reference.artifact_id in seen:
                    continue
                seen.add(reference.artifact_id)
                metadata = self.artifacts.get_metadata(reference.artifact_id)
                if metadata.task_id != task_id or metadata.trace_id != trace_id:
                    return False, f"artifact {reference.artifact_id} has wrong task or trace"
                if metadata.type is not reference.type or metadata.sha256 != reference.sha256:
                    return False, f"artifact {reference.artifact_id} metadata does not match reference"
                for _ in self.artifacts.iter_bytes(reference.artifact_id):
                    pass
            if not self._verification_binding_matches(verification):
                return False, "persisted verification report does not match structured evidence"
            if not self._review_binding_matches(review):
                return False, "persisted review report does not match structured evidence"
        except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError) as exc:
            return False, f"evidence validation failed: {exc}"
        return True, f"validated {len(seen)} integrity-bound evidence artifacts"

    @staticmethod
    def _evidence_references(
        verification: VerificationReport, review: ReviewReport
    ) -> tuple[ArtifactReference, ...]:
        references = [
            verification.artifact,
            verification.change_set.manifest_artifact,
            verification.permission_report.artifact,
            review.artifact,
        ]
        if verification.change_set.diff_artifact is not None:
            references.append(verification.change_set.diff_artifact)
        for check in verification.checks:
            references.extend(check.evidence)
        for result in verification.command_results:
            references.append(result.audit_artifact)
            if result.stdout_artifact is not None:
                references.append(result.stdout_artifact)
            if result.stderr_artifact is not None:
                references.append(result.stderr_artifact)
        return tuple(references)

    def _verification_binding_matches(self, report: VerificationReport) -> bool:
        content = self.artifacts.read_json(report.artifact.artifact_id)
        if not isinstance(content, dict):
            return False
        return (
            content.get("task_id") == str(report.task_id)
            and content.get("trace_id") == str(report.trace_id)
            and content.get("passed") is report.passed
            and content.get("checks")
            == [check.model_dump(mode="json") for check in report.checks]
        )

    def _review_binding_matches(self, report: ReviewReport) -> bool:
        content = self.artifacts.read_json(report.artifact.artifact_id)
        if not isinstance(content, dict):
            return False
        return (
            content.get("task_id") == str(report.task_id)
            and content.get("trace_id") == str(report.trace_id)
            and content.get("reviewer") == report.reviewer
            and content.get("verdict") == report.verdict.value
            and content.get("issues")
            == [issue.model_dump(mode="json") for issue in report.issues]
        )
