"""Internal staged recovery, deliberately no HTTP execution or CLI dispatch."""

from dataclasses import dataclass
from uuid import UUID

from app.api.continuation_runtime import HumanContinuationKernel
from app.api.models import ContinueTaskPreflightRequest
from app.api.service import TaskStateConflict
from app.storage import ArtifactReference
from app.storage.continuation_resumptions import ClaimedResumption
from app.storage.continuations import ContinuationConflictError, human_message_digest
from app.team.execution import WorkflowRuntime
from app.team.models import StoredChatMessage


@dataclass(frozen=True)
class PreparedResumption:
    record: ClaimedResumption
    runtime: WorkflowRuntime | None = None
    source: StoredChatMessage | None = None
    historical_references: tuple[ArtifactReference, ...] = ()


class ControlledResumptionKernel:
    def __init__(self, service):
        self.service = service

    async def resume(self, task_id: UUID, authorization_id: UUID) -> PreparedResumption:
        if not isinstance(task_id, UUID) or not isinstance(authorization_id, UUID):
            raise TypeError("task and authorization IDs must be UUIDs")
        service = self.service
        async with service._lock:
            receipt = service.continuation_resumptions.get(task_id=task_id, authorization_id=authorization_id)
            if receipt is not None:
                return PreparedResumption(ClaimedResumption(receipt, replayed=True))
            grant = service.continuation_authorizations.get(task_id=task_id, authorization_id=authorization_id)
            intent = grant.intent
            prepared = await HumanContinuationKernel(service)._prepare(task_id, ContinueTaskPreflightRequest(
                expected_revision=intent.expected_revision, message_id=intent.message_id, target_role=intent.target_role.value,
            ))
            if (prepared.checkpoint.runtime_revision != intent.runtime_revision
                    or prepared.references != grant.artifacts
                    or prepared.checkpoint.target_member_id != intent.target_member_id
                    or prepared.source.deliveries[0].recipient_id != intent.source_recipient_id
                    or prepared.checkpoint.correlation_id != intent.correlation_id
                    or human_message_digest(prepared.source.message.model_dump(mode="json")) != intent.source_sha256):
                raise ContinuationConflictError("authorization inputs changed before recovery")
            def validate_budget():
                task = service.tasks.get(task_id).task
                if task.rework_rounds >= service.event_loop.controller.max_rework_rounds:
                    raise TaskStateConflict("rework budget exhausted")
                guard = service.event_loop.executor.budget_guard
                violation = guard.evaluate(task_id, room_id=intent.room_id)
                if violation is not None:
                    raise TaskStateConflict(f"conversation budget blocked recovery: {violation.code.value}")
                if guard.usage(task_id, room_id=intent.room_id).room_messages + 1 >= guard.policy.max_room_messages:
                    raise TaskStateConflict("continuation handoff would exhaust the message budget")
            record = service.continuation_resumptions.consume(
                task_id=task_id, authorization_id=authorization_id,
                references=prepared.references, validate_budget=validate_budget,
            )
            if record.replayed:
                return PreparedResumption(record)
            # Do not rehydrate old verification/completion or native sessions.
            # Reviewer first enters VERIFYING; no old approval can complete it.
            runtime = WorkflowRuntime.from_context(record.task.task, record.context)
            return PreparedResumption(record, runtime, prepared.source, prepared.references)
