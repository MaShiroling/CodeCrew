import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents import (
    AgentCapability,
    AgentExitReason,
    AgentRegistry,
    AgentRequest,
    AgentRole,
    PermissionMode,
)
from app.orchestration.models import Task
from app.orchestration.orchestrator import ReviewDraft
from app.storage import ArtifactReference, ArtifactStore
from app.verification import ReviewIssue, ReviewVerdict, VerificationReport
from app.workspace import WorktreeHandle


class ReviewerExecutionError(RuntimeError):
    """Raised when an independent reviewer session does not return valid output."""


class _StructuredReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdict: ReviewVerdict
    issues: tuple[ReviewIssue, ...] = ()
    summary: str = Field(min_length=1, max_length=4000)


class AgentReviewerRunner:
    """Run each review in a new read-only adapter session."""

    def __init__(
        self,
        registry: AgentRegistry,
        artifacts: ArtifactStore,
        *,
        agent_name: str = "claude-code",
        timeout_seconds: int = 900,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry = registry
        self.artifacts = artifacts
        self.agent_name = agent_name
        self.timeout_seconds = timeout_seconds

    async def review(
        self,
        task: Task,
        worktree: WorktreeHandle,
        plan: ArtifactReference,
        verification: VerificationReport,
    ) -> ReviewDraft:
        self._validate_inputs(task, worktree, plan, verification)
        request = AgentRequest(
            task_id=task.id,
            trace_id=task.trace_id,
            role=AgentRole.REVIEWER,
            prompt=self._prompt(task, plan, verification),
            working_directory=worktree.worktree_path,
            permission_mode=PermissionMode.READ_ONLY,
            timeout_seconds=self.timeout_seconds,
            metadata={
                "plan_artifact_id": str(plan.artifact_id),
                "verification_artifact_id": str(verification.artifact.artifact_id),
                "review_round": task.rework_rounds,
                "independent_session": True,
            },
        )
        async with self.registry.acquire(
            self.agent_name,
            role=AgentRole.REVIEWER,
            permission_mode=PermissionMode.READ_ONLY,
            required_capabilities={AgentCapability.CODE_REVIEW},
        ) as adapter:
            session = await adapter.start(request)
            result = await adapter.wait(session.session_id)
        if result.reason is not AgentExitReason.COMPLETED or result.exit_code not in {0, None}:
            detail = result.error or result.reason.value
            raise ReviewerExecutionError(f"reviewer session failed: {detail}")
        review = self._parse_output(result.output)
        return ReviewDraft(
            reviewer=self.agent_name,
            verdict=review.verdict,
            issues=review.issues,
            summary=review.summary,
        )

    def _prompt(
        self,
        task: Task,
        plan: ArtifactReference,
        verification: VerificationReport,
    ) -> str:
        references = {
            "plan": self.artifacts.blob_path_for(plan.artifact_id),
            "verification": self.artifacts.blob_path_for(
                verification.artifact.artifact_id
            ),
            "changeset": self.artifacts.blob_path_for(
                verification.change_set.manifest_artifact.artifact_id
            ),
        }
        if verification.change_set.diff_artifact is not None:
            references["diff"] = self.artifacts.blob_path_for(
                verification.change_set.diff_artifact.artifact_id
            )
        rendered = "\n".join(f"- {name}: {path}" for name, path in references.items())
        return (
            "You are the independent read-only CodeCrew reviewer. Do not modify files.\n"
            f"Issue:\n{task.issue}\n\n"
            "Inspect the worktree and these immutable evidence artifacts:\n"
            f"{rendered}\n\n"
            "Read each listed artifact before deciding; do not rely on a summary alone.\n"
            "Return only one JSON object with keys: verdict ('approved' or 'rejected'), "
            "summary, and issues. Each issue must contain priority "
            "('low', 'medium', 'high', or 'critical'), summary, and resolved. "
            "Approve only when the code Diff and verification evidence support the Issue; "
            "if evidence is missing, failed, or uncertain, reject and explain why."
        )

    @staticmethod
    def _parse_output(output: dict[str, Any]) -> _StructuredReview:
        candidate: Any = output.get("review")
        if candidate is None:
            candidate = output.get("result", output.get("message"))
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise ReviewerExecutionError("reviewer output is not valid JSON") from exc
        if not isinstance(candidate, dict):
            raise ReviewerExecutionError("reviewer output does not contain a review object")
        try:
            return _StructuredReview.model_validate(candidate)
        except ValidationError as exc:
            raise ReviewerExecutionError(f"invalid structured review: {exc}") from exc

    def _validate_inputs(
        self,
        task: Task,
        worktree: WorktreeHandle,
        plan: ArtifactReference,
        verification: VerificationReport,
    ) -> None:
        if worktree.task_id != task.id or verification.task_id != task.id:
            raise ReviewerExecutionError("review inputs belong to another task")
        if verification.trace_id != task.trace_id:
            raise ReviewerExecutionError("review evidence belongs to another trace")
        plan_metadata = self.artifacts.get_metadata(plan.artifact_id)
        if plan_metadata.task_id != task.id or plan_metadata.trace_id != task.trace_id:
            raise ReviewerExecutionError("plan artifact belongs to another task or trace")
