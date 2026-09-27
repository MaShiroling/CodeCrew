"""Internal preparation/single-turn kernel; intentionally not an HTTP handler.

SQLite claims prevent repeated dispatch across repository/service instances.
Claimed attempts are never auto-retried; HTTP dispatch uses a separate coordinator.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from app.api.continuation import preflight_continuation
from app.api.details import ContinueTaskPreflight
from app.api.models import ContinueTaskPreflightRequest
from app.api.service import TaskDetailUnavailable, TaskServiceUnavailable, TaskStateConflict
from app.recovery import EvidenceRecoveryService, RecoveredEvidence
from app.storage import ArtifactReference, ArtifactType
from app.storage.continuation_cancellations import CancellationObservation
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntent,
    ContinuationReceipt,
    ContinuationState,
    human_message_digest,
)
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
    prepared: PreparedContinuation | None
    result: DirectiveExecutionResult | None
    runtime_revision: int | None
    receipt: ContinuationReceipt
    replayed: bool = False


class HumanContinuationKernel:
    """Trusted internal API, exercised with Fake adapters in this substep.

    Restores context, not an executable task state: needs_human remains parked.
    Outputs remain in the room for future explicit controller processing. Old
    verification is historical input, never a new success certificate.
    """

    def __init__(self, service) -> None:
        self.service = service

    async def prepare(
        self,
        task_id: UUID,
        request: ContinueTaskPreflightRequest,
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
            raise TaskDetailUnavailable(
                "persisted verification plan differs from configured policy"
            )
        if runtime.agent_names != service.agent_names:
            raise TaskDetailUnavailable("persisted Agent bindings differ from configured bindings")
        # A persisted name alone is insufficient: check the real registry and
        # the runner's capability/permission contract before creating messages.
        executor.turns.validate_binding(
            request.target_role, runtime.agent_names[request.target_role]
        )
        handle = await service.worktrees.inspect(task_id)
        if (
            handle != runtime.worktree
            or handle.repository_root.resolve() != Path(task.repository_path).resolve()
        ):
            raise TaskDetailUnavailable("managed worktree differs from persisted task context")
        # Git inspection awaits; reject task/context/message/budget changes
        # instead of accepting a stale read-only preflight result.
        if preflight_continuation(service, task_id, request) != checkpoint:
            raise TaskStateConflict("continuation inputs changed during runtime preparation")
        if (
            runtime.verification_plan != service.verification_plan
            or runtime.agent_names != service.agent_names
        ):
            raise TaskDetailUnavailable("configured workflow changed during runtime preparation")
        executor.turns.validate_binding(
            request.target_role, runtime.agent_names[request.target_role]
        )
        evidence = EvidenceRecoveryService(
            service.router.artifacts, service.router.trace_store
        ).restore_runtime(runtime)
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
            references.append(
                ArtifactReference.from_metadata(metadata, summary=f"Plan v{plan.version}")
            )
        elif request.target_role is not MemberRole.PLANNER:
            raise TaskDetailUnavailable(
                "implementation/review continuation requires a current Plan"
            )
        if evidence.verification is not None:
            if evidence.verification.change_set.base_revision != handle.base_revision:
                raise TaskDetailUnavailable(
                    "verification evidence uses a different worktree baseline"
                )
            references.extend(executor._review_evidence(runtime, evidence.verification))
        if evidence.review is not None:
            references.append(evidence.review.artifact)
        if request.target_role is MemberRole.REVIEWER and (
            evidence.verification is None or evidence.verification.change_set.diff_artifact is None
        ):
            raise TaskDetailUnavailable(
                "review continuation requires verification and Diff evidence"
            )
        unique = {reference.artifact_id: reference for reference in references}
        if len(unique) > MAX_CHAT_ARTIFACTS:
            raise TaskDetailUnavailable("continuation evidence exceeds the reference limit")
        for reference in unique.values():
            metadata = service.router.artifacts.get_metadata(reference.artifact_id)
            if (
                metadata.task_id != task.id
                or metadata.trace_id != task.trace_id
                or metadata.type != reference.type
                or metadata.sha256 != reference.sha256
            ):
                raise TaskDetailUnavailable(
                    "continuation evidence does not match task-bound reference"
                )
            service.router.artifacts.read_bytes(reference.artifact_id)
        # An Implementer can invalidate old test results. Keep references for
        # context, but don't expose them as a current runtime verification.
        if request.target_role is MemberRole.IMPLEMENTER:
            runtime.latest_verification = None
        runtime.native_session_ids.pop(request.target_role, None)
        return PreparedContinuation(
            checkpoint,
            runtime,
            service.rooms.get_message(request.message_id),
            evidence,
            tuple(unique.values()),
        )

    async def run_single(
        self,
        task_id: UUID,
        request: ContinueTaskPreflightRequest,
        *,
        idempotency_key: UUID | None = None,
    ) -> ContinuationTurn:
        """One durably claimed turn, no automatic workflow or model retries.

        Omitted keys use a deterministic per-Human-message UUID for internal
        callers. A future HTTP contract must require an explicit UUID key.
        Replays return only the stored receipt, not reconstructed live objects.
        """
        service = self.service
        if idempotency_key is not None and not isinstance(idempotency_key, UUID):
            raise ValueError("continuation idempotency key must be a UUID")
        key = idempotency_key or uuid5(task_id, f"human-continuation:{request.message_id}")
        async with service._lock:
            repository = service.continuations
            existing = repository.get_by_key(task_id, key)
            if existing is None:
                active = repository.active_for_task(task_id)
                if active is not None:
                    if active.receipt.request.idempotency_key != key:
                        raise ContinuationConflictError("task has an unresolved continuation reservation")
                    existing = active
            if existing is not None:
                repository.require_same_command(
                    existing,
                    task_id=task_id,
                    key=key,
                    message_id=request.message_id,
                    target_role=request.target_role,
                    expected_revision=request.expected_revision,
                )
                if existing.receipt.state is not ContinuationState.PENDING:
                    return self._replay(existing.receipt)
            try:
                prepared = await self._prepare(task_id, request)
            except TaskStateConflict:
                # Another service may claim/finish the same key while Git
                # inspection awaits. Replay its durable outcome, not the stale
                # preparation or a second Agent call.
                concurrent = repository.get_by_key(task_id, key)
                if concurrent is not None and concurrent.receipt.state is not ContinuationState.PENDING:
                    repository.require_same_command(
                        concurrent, task_id=task_id, key=key, message_id=request.message_id,
                        target_role=request.target_role, expected_revision=request.expected_revision,
                    )
                    return self._replay(concurrent.receipt)
                raise
            guard = service.event_loop.executor.budget_guard
            usage = guard.usage(task_id, room_id=prepared.runtime.room_id)
            if usage.room_messages + 1 >= guard.policy.max_room_messages:
                raise TaskStateConflict("continuation handoff would exhaust the message budget")
            checkpoint = prepared.checkpoint
            intent = ContinuationIntent(
                idempotency_key=key,
                task_id=task_id,
                trace_id=checkpoint.trace_id,
                room_id=prepared.runtime.room_id,
                message_id=request.message_id,
                source_sha256=human_message_digest(prepared.source.message.model_dump(mode="json")),
                correlation_id=checkpoint.correlation_id,
                target_role=request.target_role.value,
                target_member_id=checkpoint.target_member_id,
                source_recipient_id=prepared.source.deliveries[0].recipient_id,
                agent_name=prepared.runtime.agent_names[request.target_role],
                expected_revision=checkpoint.task_revision,
                runtime_revision=checkpoint.runtime_revision,
            )
            registered = repository.register(intent)
            # Pending retries cannot silently rebase their captured runtime.
            if registered.receipt.request.runtime_revision != checkpoint.runtime_revision:
                raise ContinuationConflictError("pending continuation runtime revision changed")
            claim = repository.claim(registered.receipt.request.request_id)
            if claim is None:
                return self._replay(repository.get(registered.receipt.request.request_id).receipt)
            operation_id = claim.receipt.request.request_id
            service._continuation_runs[operation_id] = (claim, asyncio.current_task())
            primary_error = None
            try:
                return await self._run_claimed(prepared, claim)
            except asyncio.CancelledError as exc:
                primary_error = exc
                self._pause_after_error(claim, "cancelled", exc)
                raise
            except Exception as exc:
                primary_error = exc
                self._pause_after_error(claim, "execution_failed", exc)
                raise
            finally:
                service._continuation_runs.pop(operation_id, None)
                try:
                    # Cancellation can arrive while waiting for registry/inputs,
                    # before an adapter session exists or its observer is entered.
                    service.continuation_cancellations.observe(
                        claim, CancellationObservation(outcome="no_observation"),
                    )
                except Exception as diagnostic_error:
                    if primary_error is not None:
                        primary_error.add_note(f"Cancellation finalization failed: {type(diagnostic_error).__name__}")
                    else:
                        raise

    async def _run_claimed(self, prepared, claim, *, park_resumed=False) -> ContinuationTurn:
        service = self.service
        runtime, source = prepared.runtime, prepared.source
        task_id = runtime.task.id
        executor = service.event_loop.executor
        room = service.rooms.get_room(runtime.room_id)
        target = service.rooms.get_member(prepared.checkpoint.target_member_id)
        orchestrator = next(m for m in room.members if m.role is MemberRole.ORCHESTRATOR)
        handoff = service.router.route(
            ChatMessage(
                task_id=task_id,
                trace_id=runtime.task.trace_id,
                room_id=runtime.room_id,
                sender_id=orchestrator.member_id,
                recipients=(
                    MessageRecipient(kind=RecipientKind.MEMBER, member_id=target.member_id),
                ),
                type=MessageType.SYSTEM_EVENT,
                # Preserve the complete allowed Human body, including its max
                # length. Control boundaries are code, not appended prose that
                # could overflow the room limit or be mistaken for authority.
                content=source.message.content,
                artifacts=prepared.references,
                correlation_id=source.message.correlation_id,
                causation_id=source.message.message_id,
                idempotency_key=f"human-continuation:{source.message.message_id}:{target.role.value}",
            ),
            authenticated_sender_id=orchestrator.member_id,
        )
        input_ids = (handoff.message.message_id,)
        if source.deliveries[0].recipient_id == target.member_id:
            input_ids = (source.message.message_id, *input_ids)
        result = await executor.execute_single_turn(
            runtime=runtime,
            source=source,
            target=target,
            input_message_ids=input_ids,
            validate_before_routing=self._historical_evidence_only,
            acknowledge_inputs=False,
            observe_cancellation=lambda observation: self._record_cancellation(claim, observation),
            cancellation_timeout_seconds=service.continuation_cancellation_timeout_seconds,
        )
        if service.continuation_cancellations.get(task_id=task_id, request_id=claim.receipt.request.request_id) is not None:
            raise asyncio.CancelledError("persisted continuation cancellation forbids final commit")
        if result.agent_turns:
            turn = result.agent_turns[0]
            receipt = service.continuations.finish(
                claim,
                context=runtime.to_context(),
                session=turn.session,
                input_ids=turn.consumed_message_ids,
                output_ids=tuple(message.message.message_id for message in turn.routed_messages),
                park_resumed=park_resumed,
            )
        else:
            receipt = service.continuations.pause(claim, code="budget_blocked", park_resumed=park_resumed)
        return ContinuationTurn(prepared, result, receipt.runtime_revision, receipt)

    @staticmethod
    def _replay(receipt) -> ContinuationTurn:
        return ContinuationTurn(None, None, receipt.runtime_revision, receipt, replayed=True)

    def _pause_after_error(self, claim, code, error) -> None:
        try:
            self.service.continuations.pause(claim, code=code)
        except Exception as diagnostic_error:  # noqa: BLE001 - preserve the original error and durable claim.
            # If SQLite is unavailable, the durable CLAIMED record still blocks
            # replay. Never hide the original error or reset its ownership.
            error.add_note(
                f"continuation pause recording failed: {type(diagnostic_error).__name__}"
            )

    def _record_cancellation(self, claim, observation):
        service = self.service
        intent = claim.receipt.request
        if service.continuation_cancellations.get(task_id=intent.task_id, request_id=intent.request_id) is None:
            return
        reference = None
        if observation.result is not None:
            result = observation.result
            session = observation.session
            if (session is None or session.task_id != intent.task_id or session.trace_id != intent.trace_id
                    or session.agent_name != intent.agent_name or session.role != intent.target_role
                    or result.session_id != session.session_id or result.trace_id != intent.trace_id):
                raise AgentTurnError("cancellation result does not match owned session")
            artifact = service.router.artifacts.put_json(
                result.model_dump(mode="json"), task_id=intent.task_id, trace_id=intent.trace_id,
                type=ArtifactType.GENERIC, created_by="cancellation-observer",
                filename=f"cancellation-result-{intent.request_id}.json",
            )
            reference = ArtifactReference.from_metadata(artifact, summary="Adapter terminal result after cancellation; not OS tree-stop proof")
        service.continuation_cancellations.observe(claim, CancellationObservation(
            outcome=observation.outcome,
            session_id=observation.session.session_id if observation.session else None,
            result_artifact=reference,
            exit_reason=observation.result.reason if observation.result else None,
            exit_code=observation.result.exit_code if observation.result else None,
        ))

    @staticmethod
    def _historical_evidence_only(candidate, turn) -> None:
        if any(
            action.action in {ChatActionType.APPROVE_REVIEW, ChatActionType.REQUEST_REWORK}
            for action in turn.actions
        ):
            raise AgentTurnError(
                "Human follow-up cannot issue review decisions from historical evidence; "
                "a fresh controlled verification/review path is required"
            )
