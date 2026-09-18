import os
from enum import Enum
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.orchestration.models import utc_now
from app.storage import ArtifactReference, ArtifactStore, ArtifactType
from app.workspace.changes import WorkspaceChangeSet
from app.workspace.worktrees import WorktreeHandle


class WorkspacePermissionError(RuntimeError):
    """Raised when permission evidence does not belong to the inspected workspace."""


class PermissionViolationKind(str, Enum):
    OUTSIDE_ALLOWED_PATHS = "outside_allowed_paths"
    DENIED_PATH = "denied_path"
    SYMLINK_ESCAPE = "symlink_escape"


class PathRole(str, Enum):
    CURRENT = "current"
    PREVIOUS = "previous"


class PermissionPolicy(BaseModel):
    """Repository-relative write roots with deny rules taking precedence."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    allowed_paths: tuple[str, ...] = Field(min_length=1)
    denied_paths: tuple[str, ...] = (".git", ".codecrew", ".env")

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def validate_rules(cls, rules: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for rule in rules:
            if "\\" in rule:
                raise ValueError("permission paths must use forward slashes")
            path = PurePosixPath(rule)
            if path.is_absolute() or ".." in path.parts or not rule.strip():
                raise ValueError("permission paths must be repository-relative")
            value = path.as_posix()
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized)


class PermissionViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    path_role: PathRole
    kind: PermissionViolationKind
    rule: str | None = None
    resolved_path: str | None = None
    reason: str = Field(min_length=1)


class PermissionReport(BaseModel):
    """Deterministic authorization result consumed by the verifier and completion guard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    policy: PermissionPolicy
    checked_paths: tuple[str, ...]
    violations: tuple[PermissionViolation, ...]
    passed: bool
    artifact: ArtifactReference
    checked_at: AwareDatetime = Field(default_factory=utc_now)


class PermissionGate:
    def __init__(self, artifacts: ArtifactStore, policy: PermissionPolicy) -> None:
        self.artifacts = artifacts
        self.policy = policy

    def check(
        self,
        handle: WorktreeHandle,
        changes: WorkspaceChangeSet,
        *,
        created_by: str = "verifier",
    ) -> PermissionReport:
        if changes.task_id != handle.task_id:
            raise WorkspacePermissionError("change set belongs to another task")
        if changes.base_revision != handle.base_revision:
            raise WorkspacePermissionError("change set baseline does not match worktree")
        worktree = handle.worktree_path.resolve()
        if not worktree.is_dir():
            raise WorkspacePermissionError(f"worktree directory does not exist: {worktree}")

        checked: list[str] = []
        violations: list[PermissionViolation] = []
        for changed_file in changes.changed_files:
            self._check_path(
                worktree,
                changed_file.path,
                PathRole.CURRENT,
                checked,
                violations,
                inspect_physical=True,
            )
            if changed_file.old_path is not None:
                self._check_path(
                    worktree,
                    changed_file.old_path,
                    PathRole.PREVIOUS,
                    checked,
                    violations,
                    inspect_physical=False,
                )

        unique_violations = tuple(
            {
                (item.path, item.path_role, item.kind, item.rule, item.resolved_path): item
                for item in violations
            }.values()
        )
        checked_paths = tuple(dict.fromkeys(checked))
        passed = not unique_violations
        report_content = {
            "task_id": str(handle.task_id),
            "trace_id": str(changes.trace_id),
            "base_revision": changes.base_revision,
            "policy": self.policy.model_dump(mode="json"),
            "checked_paths": list(checked_paths),
            "violations": [item.model_dump(mode="json") for item in unique_violations],
            "passed": passed,
        }
        metadata = self.artifacts.put_json(
            report_content,
            task_id=handle.task_id,
            trace_id=changes.trace_id,
            type=ArtifactType.PERMISSION_REPORT,
            created_by=created_by,
            filename="permission-report.json",
            metadata={"base_revision": changes.base_revision},
        )
        reference = ArtifactReference.from_metadata(
            metadata,
            summary=(
                "Workspace permission check passed"
                if passed
                else f"Workspace permission check found {len(unique_violations)} violations"
            ),
        )
        return PermissionReport(
            task_id=handle.task_id,
            trace_id=changes.trace_id,
            policy=self.policy,
            checked_paths=checked_paths,
            violations=unique_violations,
            passed=passed,
            artifact=reference,
        )

    def _check_path(
        self,
        worktree: Path,
        relative_path: str,
        role: PathRole,
        checked: list[str],
        violations: list[PermissionViolation],
        *,
        inspect_physical: bool,
    ) -> None:
        checked.append(relative_path)
        lexical_violation = self._policy_violation(relative_path, role)
        if lexical_violation is not None:
            violations.append(lexical_violation)

        candidate = worktree / relative_path
        if not inspect_physical or not os.path.lexists(candidate):
            return
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            violations.append(
                PermissionViolation(
                    path=relative_path,
                    path_role=role,
                    kind=PermissionViolationKind.SYMLINK_ESCAPE,
                    reason=f"path cannot be resolved safely: {exc}",
                )
            )
            return
        if not resolved.is_relative_to(worktree):
            violations.append(
                PermissionViolation(
                    path=relative_path,
                    path_role=role,
                    kind=PermissionViolationKind.SYMLINK_ESCAPE,
                    resolved_path=str(resolved),
                    reason="path resolves outside the managed worktree",
                )
            )
            return

        resolved_relative = resolved.relative_to(worktree).as_posix()
        if resolved_relative != relative_path:
            target_violation = self._policy_violation(resolved_relative, role)
            if target_violation is not None:
                violations.append(
                    PermissionViolation(
                        path=relative_path,
                        path_role=role,
                        kind=PermissionViolationKind.SYMLINK_ESCAPE,
                        rule=target_violation.rule,
                        resolved_path=resolved_relative,
                        reason="path resolves into an unauthorized repository location",
                    )
                )

    def _policy_violation(
        self, relative_path: str, role: PathRole
    ) -> PermissionViolation | None:
        for rule in self.policy.denied_paths:
            if self._matches(relative_path, rule):
                return PermissionViolation(
                    path=relative_path,
                    path_role=role,
                    kind=PermissionViolationKind.DENIED_PATH,
                    rule=rule,
                    reason=f"path is denied by rule {rule!r}",
                )
        if not any(self._matches(relative_path, rule) for rule in self.policy.allowed_paths):
            return PermissionViolation(
                path=relative_path,
                path_role=role,
                kind=PermissionViolationKind.OUTSIDE_ALLOWED_PATHS,
                reason="path is outside all allowed write roots",
            )
        return None

    @staticmethod
    def _matches(relative_path: str, rule: str) -> bool:
        if rule == ".":
            return True
        path = PurePosixPath(relative_path)
        root = PurePosixPath(rule)
        return path == root or root in path.parents
