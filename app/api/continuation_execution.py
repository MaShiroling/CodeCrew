"""HTTP admission and service-owned asynchronous single-turn execution.

Durable claims provide at-most-once dispatch, not exactly-once execution.
No restart adoption, workflow auto-chain, historical approval or budget reset.
"""

import asyncio
import logging

from app.api.continuation_runtime import HumanContinuationKernel
from app.api.controlled_resumption import ControlledResumptionKernel
from app.api.service import TaskServiceUnavailable, TaskStateConflict
from app.storage.continuation_cancellations import CancellationObservation
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntent,
    ContinuationState,
    human_message_digest,
)
from app.team.models import MemberRole

logger = logging.getLogger(__name__)


class ContinuationExecutionCoordinator:
    def __init__(self, service):
        self.service = service
        self.kernel = HumanContinuationKernel(service)

    async def accept(self, task_id, request, *, workflow=False):
        service = self.service
        scope = "controlled-workflow-continuation" if workflow else "single-agent-continuation"
        if workflow and request.target_role is MemberRole.REVIEWER:
            raise TaskStateConflict("workflow continuation must begin with Planner or Implementer")
        digest = human_message_digest(
            {"scope": scope, "command": request.model_dump(mode="json")}
            if workflow else request.model_dump(mode="json")
        )
        async with service._lock:
            existing = service.continuations.get_by_key(task_id, request.idempotency_key)
            if existing is not None:
                return self._replay(existing, digest, scope=scope)
            if not service._continuation_accepting:
                raise TaskServiceUnavailable("continuation admission is closed during shutdown")
            try:
                prepared, claim, resumed = await self._admit(task_id, request, digest, scope=scope)
            except (ContinuationConflictError, TaskStateConflict):
                # Git inspection awaits: another service may atomically admit
                # the same command meanwhile. It owns dispatch; this is replay.
                concurrent = service.continuations.get_by_key(task_id, request.idempotency_key)
                if concurrent is not None:
                    return self._replay(concurrent, digest, scope=scope)
                raise
            if claim is None:
                existing = service.continuations.get_by_key(task_id, request.idempotency_key)
                return self._replay(existing, digest, scope=scope)
            prepared.runtime.latest_verification = None
            prepared.runtime.latest_completion = None
            prepared.runtime.native_session_ids.clear()
            operation_id = claim.receipt.request.request_id
            # No await between committed admission and registering local owner.
            worker = asyncio.create_task(
                (self.kernel._run_workflow_claimed if workflow else self.kernel._run_claimed)(
                    prepared, claim, park_resumed=resumed,
                ),
                name=f"codecrew-continuation-{operation_id}",
            )
            service._continuation_runs[operation_id] = (claim, worker)
            worker.add_done_callback(lambda run: self._finalize(run, claim, resumed=resumed))
            return service.continuations.status(task_id=task_id, request_id=operation_id)

    def _replay(self, record, digest, *, scope):
        if record is None:
            raise ContinuationConflictError("consumed authorization has no HTTP admission")
        repository = self.service.continuations
        repository.http_replay(record, digest, scope=scope)
        intent = record.receipt.request
        return repository.status(task_id=intent.task_id, request_id=intent.request_id)

    async def _admit(self, task_id, request, digest, *, scope):
        service = self.service
        if request.authorization_id is not None:
            grant = service.continuation_authorizations.get(task_id=task_id, authorization_id=request.authorization_id)
            intent = grant.intent
            if (intent.idempotency_key != request.idempotency_key or intent.message_id != request.message_id
                    or intent.target_role.value != request.target_role.value or intent.expected_revision != request.expected_revision):
                raise ContinuationConflictError("HTTP command does not match its authorization")
            result = await ControlledResumptionKernel(service)._resume(
                task_id, request.authorization_id, http_command_sha256=digest,
                http_scope=scope,
            )
            return result.continuation, result.record.claim, True
        prepared = await self.kernel._prepare(task_id, request)
        if not service._continuation_accepting:
            raise TaskServiceUnavailable("continuation admission closed during preparation")
        guard = service.event_loop.executor.budget_guard
        if guard.usage(task_id, room_id=prepared.runtime.room_id).room_messages + 1 >= guard.policy.max_room_messages:
            raise TaskStateConflict("continuation handoff would exhaust the message budget")
        checkpoint = prepared.checkpoint
        intent = ContinuationIntent(
            idempotency_key=request.idempotency_key, task_id=task_id, trace_id=checkpoint.trace_id,
            room_id=prepared.runtime.room_id, message_id=request.message_id,
            source_sha256=human_message_digest(prepared.source.message.model_dump(mode="json")),
            correlation_id=checkpoint.correlation_id, target_role=request.target_role.value,
            target_member_id=checkpoint.target_member_id, source_recipient_id=prepared.source.deliveries[0].recipient_id,
            agent_name=prepared.runtime.agent_names[request.target_role], expected_revision=checkpoint.task_revision,
            runtime_revision=checkpoint.runtime_revision,
        )
        return prepared, service.continuations.admit_first(intent, command_sha256=digest, scope=scope), False

    def _finalize(self, worker, claim, *, resumed):
        """Also runs if cancelled before coroutine entry; never release ownership."""
        service = self.service
        try:
            error = asyncio.CancelledError() if worker.cancelled() else worker.exception()
            if error is not None:
                current = service.continuations.get(claim.receipt.request.request_id)
                if current.receipt.state is ContinuationState.CLAIMED:
                    service.continuations.pause(
                        claim, code="cancelled" if worker.cancelled() else "execution_failed", park_resumed=resumed,
                    )
            service.continuation_cancellations.observe(claim, CancellationObservation(outcome="no_observation"))
        except Exception as exc:  # noqa: BLE001 - leave unknown claim fenced, no background exception leak.
            logger.warning("Continuation finalization unavailable: %s", type(exc).__name__)
        finally:
            service._continuation_runs.pop(claim.receipt.request.request_id, None)
