"""Internal preparation/single-turn kernel; intentionally not an HTTP handler.

The service lock serializes local callers, not multiple processes or crash
retries. A durable continuation claim is required before exposing execution.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.api.continuation import preflight_continuation
from app.api.details import ContinueTaskPreflight
from app.api.models import ContinueTaskPreflightRequest
from app.api.service import TaskDetailUnavailable, TaskServiceUnavailable, TaskStateConflict
from app.recovery import EvidenceRecoveryService, RecoveredEvidence
from app.storage import ArtifactReference, ArtifactType
from app.team.actions import ChatActionType
from app.team.execution import DirectiveExecutionResult, WorkflowDirectiveExecutor, WorkflowRuntime
from app.team.models import (
    MAX_CHAT_ARTIFACTS,
    ChatMessage,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    StoredChatMessage,
)
from app.team.turns import AgentTurnError


@dataclass(frozen=True, slots=True)
class PreparedContinuation:
    checkpoint: ContinueTaskPreflight
    runtime: WorkflowRuntime
    source: StoredChatMessage
    evidence: RecoveredEvidence
    references: tuple[ArtifactReference, ...]


@dataclass(frozen=True, slots=True)
class ContinuationTurn:
    prepared: PreparedContinuation
    result: DirectiveExecutionResult
    runtime_revision: int


class HumanContinuationKernel:
    """Trusted internal API, exercised with Fake adapters in this substep.

    Restores context, not an executable task state: needs_human remains parked.
    Outputs remain in the room for future explicit controller processing. Old
    verification is historical input, never a new success certificate.
    """

    def __init__(self, service) -> None:
        self.service = service

    async def prepare(
        self, task_id: UUID, request: ContinueTaskPreflightRequest,
    ) -> PreparedContinuation:
        """Read-only preparation; returned data is not dispatch authorization."""
        async with self.service._lock:
            return await self._prepare(task_id, request)

    async def _prepare(self, task_id, request) -> PreparedContinuation:
        service = self.service
        checkpoint = preflight_continuation(service, task_id, request)
        executor = service.event_loop.executor
        if not isinstance(executor, WorkflowDirectiveExecutor):
            raise TaskServiceUnavailable("single-turn workflow executor is unavailable")
        context = service.contexts.get(task_id)
        task = service.tasks.get(task_id).task
        runtime = WorkflowRuntime.from_context(task, context.context)
        if runtime.verification_plan != service.verification_plan:
            raise TaskDetailUnavailable("persisted verification plan differs from configured policy")
        if runtime.agent_names != service.agent_names:
            raise TaskDetailUnavailable("persisted Agent bindings differ from configured bindings")
        # A persisted name alone is insufficient: check the real registry and
        # the runner's capability/permission contract before creating messages.
        executor.turns.validate_binding(request.target_role, runtime.agent_names[request.target_role])
        handle = await service.worktrees.inspect(task_id)
        if handle != runtime.worktree or handle.repository_root.resolve() != Path(task.repository_path).resolve():
            raise TaskDetailUnavailable("managed worktree differs from persisted task context")
        # Git inspection awaits; reject task/context/message/budget changes
        # instead of accepting a stale read-only preflight result.
        if preflight_continuation(service, task_id, request) != checkpoint:
            raise TaskStateConflict("continuation inputs changed during runtime preparation")
        if runtime.verification_plan != service.verification_plan or runtime.agent_names != service.agent_names:
            raise TaskDetailUnavailable("configured workflow changed during runtime preparation")
        executor.turns.validate_binding(request.target_role, runtime.agent_names[request.target_role])
        evidence = EvidenceRecoveryService(service.router.artifacts, service.router.trace_store).restore_runtime(runtime)
        # Preserve recovered decisions for diagnosis only. No old completion
        # decision may become a shortcut to success after Human intervention.
        runtime.latest_completion = None
        references = []
        plan = service.rooms.latest_plan_revision(runtime.room_id)
        if plan is not None:
            if plan.task_id != task.id or plan.trace_id != task.trace_id:
                raise TaskDetailUnavailable("latest Plan belongs to another task")
            metadata = service.router.artifacts.get_metadata(plan.artifact_id)
            if metadata.type is not ArtifactType.PLAN:
                raise TaskDetailUnavailable("latest Plan reference is not a Plan Artifact")
            references.append(ArtifactReference.from_metadata(metadata, summary=f"Plan v{plan.version}"))
        elif request.target_role is not MemberRole.PLANNER:
            raise TaskDetailUnavailable("implementation/review continuation requires a current Plan")
        if evidence.verification is not None:
            if evidence.verification.change_set.base_revision != handle.base_revision:
                raise TaskDetailUnavailable("verification evidence uses a different worktree baseline")
            references.extend(executor._review_evidence(runtime, evidence.verification))
        if evidence.review is not None:
            references.append(evidence.review.artifact)
        if request.target_role is MemberRole.REVIEWER and (
            evidence.verification is None or evidence.verification.change_set.diff_artifact is None
        ):
            raise TaskDetailUnavailable("review continuation requires verification and Diff evidence")
        unique = {reference.artifact_id: reference for reference in references}
        if len(unique) > MAX_CHAT_ARTIFACTS:
            raise TaskDetailUnavailable("continuation evidence exceeds the reference limit")
        for reference in unique.values():
            metadata = service.router.artifacts.get_metadata(reference.artifact_id)
            if (metadata.task_id != task.id or metadata.trace_id != task.trace_id
                    or metadata.type != reference.type or metadata.sha256 != reference.sha256):
                raise TaskDetailUnavailable("continuation evidence does not match task-bound reference")
            service.router.artifacts.read_bytes(reference.artifact_id)
        # An Implementer can invalidate old test results. Keep references for
        # context, but don't expose them as a current runtime verification.
        if request.target_role is MemberRole.IMPLEMENTER:
            runtime.latest_verification = None
        runtime.native_session_ids.pop(request.target_role, None)
        return PreparedContinuation(
            checkpoint, runtime, service.rooms.get_message(request.message_id),
            evidence, tuple(unique.values()),
        )

    async def run_single(
        self, task_id: UUID, request: ContinueTaskPreflightRequest,
    ) -> ContinuationTurn:
        """One fresh target turn, no verifier/controller/guard or implicit retry.

        Never accept a client-supplied PreparedContinuation. Recheck under the
        shared service lock for the entire local operation. Do not expose this
        method through HTTP until durable claiming/failure recovery is added.
        """
        service = self.service
        async with service._lock:
            prepared = await self._prepare(task_id, request)
            runtime, source = prepared.runtime, prepared.source
            executor = service.event_loop.executor
            guard = executor.budget_guard
            usage = guard.usage(task_id, room_id=runtime.room_id)
            if usage.room_messages + 1 >= guard.policy.max_room_messages:
                raise TaskStateConflict("continuation handoff would exhaust the message budget")
            room = service.rooms.get_room(runtime.room_id)
            target = service.rooms.get_member(prepared.checkpoint.target_member_id)
            orchestrator = next(m for m in room.members if m.role is MemberRole.ORCHESTRATOR)
            handoff = service.router.route(ChatMessage(
                task_id=task_id, trace_id=runtime.task.trace_id, room_id=runtime.room_id,
                sender_id=orchestrator.member_id,
                recipients=(MessageRecipient(kind=RecipientKind.MEMBER, member_id=target.member_id),),
                type=MessageType.SYSTEM_EVENT,
                # Preserve the complete allowed Human body, including its max
                # length. Control boundaries are code, not appended prose that
                # could overflow the room limit or be mistaken for authority.
                content=source.message.content,
                artifacts=prepared.references,
                correlation_id=source.message.correlation_id,
                causation_id=source.message.message_id,
                idempotency_key=f"human-continuation:{source.message.message_id}:{target.role.value}",
            ), authenticated_sender_id=orchestrator.member_id)
            input_ids = (handoff.message.message_id,)
            if source.deliveries[0].recipient_id == target.member_id:
                input_ids = (source.message.message_id, *input_ids)
            result = await executor.execute_single_turn(
                runtime=runtime, source=source, target=target, input_message_ids=input_ids,
                validate_before_routing=self._historical_evidence_only,
            )
            if result.agent_turns:
                context = service.contexts.save(
                    runtime.to_context(), expected_revision=prepared.checkpoint.runtime_revision,
                )
                if source.deliveries[0].recipient_id == orchestrator.member_id:
                    service.rooms.acknowledge(source.message.message_id, recipient_id=orchestrator.member_id)
                runtime_revision = context.revision
            else:
                runtime_revision = prepared.checkpoint.runtime_revision
            return ContinuationTurn(prepared, result, runtime_revision)

    @staticmethod
    def _historical_evidence_only(candidate, turn) -> None:
        if any(action.action in {ChatActionType.APPROVE_REVIEW, ChatActionType.REQUEST_REWORK}
               for action in turn.actions):
            raise AgentTurnError(
                "Human follow-up cannot issue review decisions from historical evidence; "
                "a fresh controlled verification/review path is required"
            )
