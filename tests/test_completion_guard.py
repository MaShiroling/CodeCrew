from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.storage import (
    ArtifactReference,
    ArtifactStore,
    ArtifactType,
    SQLiteDatabase,
)
from app.verification import (
    CompletionConditionKind,
    CompletionGuard,
    CompletionGuardError,
    ReviewIssue,
    ReviewIssuePriority,
    ReviewReport,
    ReviewVerdict,
    VerificationCheck,
    VerificationCheckKind,
    VerificationReport,
    VerificationStatus,
)
from app.workspace import (
    ChangedFile,
    ChangeKind,
    PermissionPolicy,
    PermissionReport,
    WorkspaceChangeSet,
)


def reference(
    store: ArtifactStore,
    task_id: UUID,
    trace_id: UUID,
    type: ArtifactType,
    content: object,
    filename: str,
) -> ArtifactReference:
    metadata = store.put_json(
        content,
        task_id=task_id,
        trace_id=trace_id,
        type=type,
        created_by="test",
        filename=filename,
    )
    return ArtifactReference.from_metadata(metadata, summary=filename)


def make_evidence(
    tmp_path: Path,
    *,
    failed_kind: VerificationCheckKind | None = None,
    verdict: ReviewVerdict = ReviewVerdict.APPROVED,
    issues: tuple[ReviewIssue, ...] = (),
    has_diff: bool = True,
):
    store = ArtifactStore(
        SQLiteDatabase(tmp_path / "codecrew.sqlite3"), tmp_path / "artifacts"
    )
    store.initialize()
    task_id = uuid4()
    trace_id = uuid4()
    manifest = reference(
        store,
        task_id,
        trace_id,
        ArtifactType.CHANGESET,
        {"changed_files": ["src/app.py"] if has_diff else []},
        "changeset.json",
    )
    diff = (
        reference(
            store,
            task_id,
            trace_id,
            ArtifactType.DIFF,
            {"patch": "diff"},
            "patch.json",
        )
        if has_diff
        else None
    )
    change_set = WorkspaceChangeSet(
        task_id=task_id,
        trace_id=trace_id,
        base_revision="a" * 40,
        head_revision="a" * 40,
        changed_files=(
            (ChangedFile(path="src/app.py", kind=ChangeKind.MODIFIED),)
            if has_diff
            else ()
        ),
        has_effective_diff=has_diff,
        diff_artifact=diff,
        manifest_artifact=manifest,
    )
    permission_artifact = reference(
        store,
        task_id,
        trace_id,
        ArtifactType.PERMISSION_REPORT,
        {"passed": failed_kind is not VerificationCheckKind.PERMISSION},
        "permission.json",
    )
    permission_passed = failed_kind is not VerificationCheckKind.PERMISSION
    permission = PermissionReport(
        task_id=task_id,
        trace_id=trace_id,
        policy=PermissionPolicy(allowed_paths=("src",)),
        checked_paths=("src/app.py",),
        violations=(),
        passed=permission_passed,
        artifact=permission_artifact,
    )
    kinds = (
        VerificationCheckKind.DIFF,
        VerificationCheckKind.PERMISSION,
        VerificationCheckKind.STATIC_ANALYSIS,
        VerificationCheckKind.PUBLIC_TESTS,
        VerificationCheckKind.HIDDEN_TESTS,
        VerificationCheckKind.COMMAND_POLICY,
    )
    checks = tuple(
        VerificationCheck(
            kind=kind,
            name=kind.value,
            status=(
                VerificationStatus.FAILED
                if kind is failed_kind or (kind is VerificationCheckKind.DIFF and not has_diff)
                else VerificationStatus.PASSED
            ),
            detail="deterministic test evidence",
            evidence=(
                permission_artifact
                if kind is VerificationCheckKind.PERMISSION
                else manifest,
            ),
        )
        for kind in kinds
    )
    verification_passed = all(
        check.status is VerificationStatus.PASSED for check in checks
    )
    verification_content = {
        "task_id": str(task_id),
        "trace_id": str(trace_id),
        "passed": verification_passed,
        "checks": [check.model_dump(mode="json") for check in checks],
    }
    verification_artifact = reference(
        store,
        task_id,
        trace_id,
        ArtifactType.VERIFICATION_REPORT,
        verification_content,
        "verification.json",
    )
    verification = VerificationReport(
        task_id=task_id,
        trace_id=trace_id,
        passed=verification_passed,
        checks=checks,
        change_set=change_set,
        permission_report=permission,
        command_results=(),
        artifact=verification_artifact,
    )
    review_content = {
        "task_id": str(task_id),
        "trace_id": str(trace_id),
        "reviewer": "claude-reviewer",
        "verdict": verdict.value,
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }
    review_artifact = reference(
        store,
        task_id,
        trace_id,
        ArtifactType.REVIEW_REPORT,
        review_content,
        "review.json",
    )
    review = ReviewReport(
        task_id=task_id,
        trace_id=trace_id,
        reviewer="claude-reviewer",
        verdict=verdict,
        issues=issues,
        summary="Structured independent review",
        artifact=review_artifact,
    )
    return store, verification, review


def test_all_guards_pass_and_decision_is_persisted(tmp_path: Path) -> None:
    store, verification, review = make_evidence(tmp_path)

    decision = CompletionGuard(store).evaluate(verification, review)

    assert decision.passed
    assert decision.failed_conditions == ()
    assert all(condition.passed for condition in decision.conditions)
    assert decision.artifact.type is ArtifactType.COMPLETION_DECISION
    persisted = store.read_json(decision.artifact.artifact_id)
    assert persisted["passed"] is True


@pytest.mark.parametrize(
    ("failed_kind", "condition"),
    [
        (VerificationCheckKind.STATIC_ANALYSIS, CompletionConditionKind.STATIC_OR_BUILD),
        (VerificationCheckKind.PUBLIC_TESTS, CompletionConditionKind.PUBLIC_TESTS),
        (VerificationCheckKind.HIDDEN_TESTS, CompletionConditionKind.HIDDEN_TESTS),
        (VerificationCheckKind.PERMISSION, CompletionConditionKind.PERMISSIONS),
        (VerificationCheckKind.COMMAND_POLICY, CompletionConditionKind.COMMAND_POLICY),
    ],
)
def test_failed_verification_conditions_block_completion(
    tmp_path: Path,
    failed_kind: VerificationCheckKind,
    condition: CompletionConditionKind,
) -> None:
    store, verification, review = make_evidence(tmp_path, failed_kind=failed_kind)

    decision = CompletionGuard(store).evaluate(verification, review)

    assert not decision.passed
    assert condition in decision.failed_conditions
    assert CompletionConditionKind.VERIFICATION in decision.failed_conditions


def test_missing_diff_blocks_completion(tmp_path: Path) -> None:
    store, verification, review = make_evidence(tmp_path, has_diff=False)

    decision = CompletionGuard(store).evaluate(verification, review)

    assert CompletionConditionKind.VALID_DIFF in decision.failed_conditions


def test_reviewer_must_explicitly_approve(tmp_path: Path) -> None:
    store, verification, review = make_evidence(
        tmp_path, verdict=ReviewVerdict.REJECTED
    )

    decision = CompletionGuard(store).evaluate(verification, review)

    assert CompletionConditionKind.REVIEW_APPROVAL in decision.failed_conditions


def test_unresolved_high_priority_issue_blocks_even_when_approved(tmp_path: Path) -> None:
    issue = ReviewIssue(
        priority=ReviewIssuePriority.HIGH,
        summary="Authorization bypass remains",
    )
    store, verification, review = make_evidence(tmp_path, issues=(issue,))

    decision = CompletionGuard(store).evaluate(verification, review)

    assert CompletionConditionKind.HIGH_PRIORITY_ISSUES in decision.failed_conditions


def test_resolved_high_and_unresolved_medium_issues_do_not_block(tmp_path: Path) -> None:
    issues = (
        ReviewIssue(
            priority=ReviewIssuePriority.HIGH,
            summary="Resolved regression",
            resolved=True,
        ),
        ReviewIssue(
            priority=ReviewIssuePriority.MEDIUM,
            summary="Optional cleanup",
        ),
    )
    store, verification, review = make_evidence(tmp_path, issues=issues)

    decision = CompletionGuard(store).evaluate(verification, review)

    assert decision.passed


def test_tampered_evidence_blocks_completion(tmp_path: Path) -> None:
    store, verification, review = make_evidence(tmp_path)
    store.blob_path_for(verification.artifact.artifact_id).write_text("tampered")

    decision = CompletionGuard(store).evaluate(verification, review)

    assert CompletionConditionKind.EVIDENCE_INTEGRITY in decision.failed_conditions


def test_persisted_report_must_match_structured_object(tmp_path: Path) -> None:
    store, verification, review = make_evidence(tmp_path)
    forged = verification.model_copy(update={"passed": False})

    decision = CompletionGuard(store).evaluate(forged, review)

    assert CompletionConditionKind.EVIDENCE_INTEGRITY in decision.failed_conditions
    assert not decision.passed


def test_inputs_must_share_task_and_trace(tmp_path: Path) -> None:
    store, verification, review = make_evidence(tmp_path)
    guard = CompletionGuard(store)

    with pytest.raises(CompletionGuardError, match="different tasks"):
        guard.evaluate(verification, review.model_copy(update={"task_id": uuid4()}))
    with pytest.raises(CompletionGuardError, match="different traces"):
        guard.evaluate(verification, review.model_copy(update={"trace_id": uuid4()}))
