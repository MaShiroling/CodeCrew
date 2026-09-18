from collections.abc import Sequence
from enum import Enum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import utc_now
from app.storage import ArtifactReference, ArtifactStore, ArtifactType
from app.workspace import (
    CommandExecutor,
    CommandRequest,
    CommandResult,
    CommandStatus,
    PermissionGate,
    PermissionReport,
    WorkspaceChangeCollector,
    WorkspaceChangeSet,
    WorktreeHandle,
)


class VerifierError(RuntimeError):
    """Raised when supplied verification evidence crosses task boundaries."""


class VerificationCheckKind(str, Enum):
    DIFF = "diff"
    PERMISSION = "permission"
    COMMAND_POLICY = "command_policy"
    STATIC_ANALYSIS = "static_analysis"
    BUILD = "build"
    PUBLIC_TESTS = "public_tests"
    HIDDEN_TESTS = "hidden_tests"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class VerificationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    kind: VerificationCheckKind
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str = "."
    timeout_seconds: float | None = Field(default=None, gt=0)
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self) -> "VerificationCommand":
        executable_kinds = {
            VerificationCheckKind.STATIC_ANALYSIS,
            VerificationCheckKind.BUILD,
            VerificationCheckKind.PUBLIC_TESTS,
            VerificationCheckKind.HIDDEN_TESTS,
        }
        if self.kind not in executable_kinds:
            raise ValueError("verification command kind must represent an executable check")
        CommandRequest(
            task_id=UUID(int=0),
            trace_id=UUID(int=0),
            argv=self.argv,
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
            environment=self.environment,
        )
        return self


class VerificationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commands: tuple[VerificationCommand, ...] = ()
    require_static_or_build: bool = True
    require_public_tests: bool = True
    require_hidden_tests: bool = True


class VerificationCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: VerificationCheckKind
    name: str = Field(min_length=1, max_length=200)
    status: VerificationStatus
    detail: str = Field(min_length=1, max_length=2000)
    command_id: UUID | None = None
    evidence: tuple[ArtifactReference, ...] = ()


class VerificationReport(BaseModel):
    """Deterministic facts; this report alone does not complete a task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    passed: bool
    checks: tuple[VerificationCheck, ...]
    change_set: WorkspaceChangeSet
    permission_report: PermissionReport
    command_results: tuple[CommandResult, ...]
    artifact: ArtifactReference
    verified_at: AwareDatetime = Field(default_factory=utc_now)


class Verifier:
    def __init__(
        self,
        artifacts: ArtifactStore,
        changes: WorkspaceChangeCollector,
        permissions: PermissionGate,
        commands: CommandExecutor,
    ) -> None:
        stores = (changes.artifacts, permissions.artifacts, commands.artifacts)
        if any(
            store.database.path != artifacts.database.path
            or store.root.resolve() != artifacts.root.resolve()
            for store in stores
        ):
            raise ValueError("verifier components must share one artifact store")
        self.artifacts = artifacts
        self.changes = changes
        self.permissions = permissions
        self.commands = commands

    async def verify(
        self,
        handle: WorktreeHandle,
        *,
        trace_id: UUID,
        plan: VerificationPlan,
        prior_command_results: Sequence[CommandResult] = (),
    ) -> VerificationReport:
        self._validate_prior_results(handle, trace_id, prior_command_results)
        initial_change_set = await self.changes.collect(
            handle, trace_id=trace_id, created_by="verifier"
        )
        initial_permission_report = self.permissions.check(handle, initial_change_set)
        command_checks: list[VerificationCheck] = []
        command_results: list[CommandResult] = []
        if initial_permission_report.passed:
            for command in plan.commands:
                result = await self.commands.execute(
                    handle,
                    CommandRequest(
                        task_id=handle.task_id,
                        trace_id=trace_id,
                        argv=command.argv,
                        working_directory=command.working_directory,
                        timeout_seconds=command.timeout_seconds,
                        environment=command.environment,
                    ),
                )
                command_results.append(result)
                command_checks.append(self._command_check(command, result))
        else:
            for command in plan.commands:
                command_checks.append(
                    VerificationCheck(
                        kind=command.kind,
                        name=command.name,
                        status=VerificationStatus.BLOCKED,
                        detail="not executed because workspace permission validation failed",
                        evidence=(initial_permission_report.artifact,),
                    )
                )

        if command_results:
            change_set = await self.changes.collect(
                handle, trace_id=trace_id, created_by="verifier"
            )
            permission_report = self.permissions.check(handle, change_set)
        else:
            change_set = initial_change_set
            permission_report = initial_permission_report

        permission_evidence = (
            (initial_permission_report.artifact,)
            if initial_permission_report.artifact.artifact_id
            == permission_report.artifact.artifact_id
            else (
                initial_permission_report.artifact,
                permission_report.artifact,
            )
        )
        checks = [
            VerificationCheck(
                kind=VerificationCheckKind.DIFF,
                name="effective_code_diff",
                status=(
                    VerificationStatus.PASSED
                    if change_set.has_effective_diff
                    else VerificationStatus.FAILED
                ),
                detail=(
                    f"detected {len(change_set.changed_files)} changed files"
                    if change_set.has_effective_diff
                    else "no effective code changes were detected"
                ),
                evidence=(change_set.manifest_artifact,)
                + ((change_set.diff_artifact,) if change_set.diff_artifact else ()),
            ),
            VerificationCheck(
                kind=VerificationCheckKind.PERMISSION,
                name="workspace_permissions",
                status=(
                    VerificationStatus.PASSED
                    if permission_report.passed
                    else VerificationStatus.FAILED
                ),
                detail=(
                    "all changed paths are authorized"
                    if permission_report.passed
                    else f"found {len(permission_report.violations)} permission violations"
                ),
                evidence=permission_evidence,
            ),
        ]
        checks.extend(command_checks)

        all_command_results = (*prior_command_results, *command_results)
        denied = tuple(
            result for result in all_command_results if result.status is CommandStatus.DENIED
        )
        checks.append(
            VerificationCheck(
                kind=VerificationCheckKind.COMMAND_POLICY,
                name="forbidden_command_attempts",
                status=(
                    VerificationStatus.FAILED if denied else VerificationStatus.PASSED
                ),
                detail=(
                    f"detected {len(denied)} denied command attempts"
                    if denied
                    else "no denied command attempts were recorded"
                ),
                evidence=tuple(result.audit_artifact for result in denied),
            )
        )
        self._append_required_checks(checks, plan)
        checks_tuple = tuple(checks)
        passed = all(check.status is VerificationStatus.PASSED for check in checks_tuple)
        report_content = {
            "task_id": str(handle.task_id),
            "trace_id": str(trace_id),
            "passed": passed,
            "checks": [check.model_dump(mode="json") for check in checks_tuple],
            "change_set_artifact_id": str(change_set.manifest_artifact.artifact_id),
            "diff_artifact_id": (
                str(change_set.diff_artifact.artifact_id)
                if change_set.diff_artifact is not None
                else None
            ),
            "permission_artifact_id": str(permission_report.artifact.artifact_id),
            "command_audit_artifact_ids": [
                str(result.audit_artifact.artifact_id) for result in all_command_results
            ],
        }
        metadata = self.artifacts.put_json(
            report_content,
            task_id=handle.task_id,
            trace_id=trace_id,
            type=ArtifactType.VERIFICATION_REPORT,
            created_by="verifier",
            filename="verification-report.json",
            metadata={"passed": str(passed).lower()},
        )
        artifact = ArtifactReference.from_metadata(
            metadata,
            summary="Deterministic verification passed" if passed else "Verification failed",
        )
        return VerificationReport(
            task_id=handle.task_id,
            trace_id=trace_id,
            passed=passed,
            checks=checks_tuple,
            change_set=change_set,
            permission_report=permission_report,
            command_results=tuple(command_results),
            artifact=artifact,
        )

    @staticmethod
    def _validate_prior_results(
        handle: WorktreeHandle,
        trace_id: UUID,
        results: Sequence[CommandResult],
    ) -> None:
        if any(result.task_id != handle.task_id for result in results):
            raise VerifierError("prior command result belongs to another task")
        if any(result.trace_id != trace_id for result in results):
            raise VerifierError("prior command result belongs to another trace")

    @staticmethod
    def _command_check(
        command: VerificationCommand, result: CommandResult
    ) -> VerificationCheck:
        status = (
            VerificationStatus.PASSED
            if result.status is CommandStatus.SUCCEEDED
            else VerificationStatus.FAILED
        )
        evidence = [result.audit_artifact]
        if result.stdout_artifact is not None:
            evidence.append(result.stdout_artifact)
        if result.stderr_artifact is not None:
            evidence.append(result.stderr_artifact)
        return VerificationCheck(
            kind=command.kind,
            name=command.name,
            status=status,
            detail=f"command finished with status {result.status.value}",
            command_id=result.command_id,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _append_required_checks(
        checks: list[VerificationCheck], plan: VerificationPlan
    ) -> None:
        command_kinds = {command.kind for command in plan.commands}
        required: list[tuple[bool, set[VerificationCheckKind], str, VerificationCheckKind]] = [
            (
                plan.require_static_or_build,
                {VerificationCheckKind.STATIC_ANALYSIS, VerificationCheckKind.BUILD},
                "static_or_build_required",
                VerificationCheckKind.STATIC_ANALYSIS,
            ),
            (
                plan.require_public_tests,
                {VerificationCheckKind.PUBLIC_TESTS},
                "public_tests_required",
                VerificationCheckKind.PUBLIC_TESTS,
            ),
            (
                plan.require_hidden_tests,
                {VerificationCheckKind.HIDDEN_TESTS},
                "hidden_tests_required",
                VerificationCheckKind.HIDDEN_TESTS,
            ),
        ]
        for enabled, accepted_kinds, name, report_kind in required:
            if enabled and command_kinds.isdisjoint(accepted_kinds):
                checks.append(
                    VerificationCheck(
                        kind=report_kind,
                        name=name,
                        status=VerificationStatus.FAILED,
                        detail="required verification command was not configured",
                    )
                )
