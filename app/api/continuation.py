"""Read-only selection/budget preflight, not a recovery or scheduling decision."""

import sqlite3

from app.api.details import ContinueTaskPreflight
from app.api.models import ContinueTaskPreflightRequest
from app.api.service import (
    TaskDetailUnavailable,
    TaskMessageInvalid,
    TaskMessageNotFound,
    TaskServiceUnavailable,
    TaskStateConflict,
)
from app.orchestration.models import TaskState
from app.team.budgets import ConversationBudgetGuard
from app.team.models import MemberKind, MemberRole, MessageDeliveryStatus, MessageType, RoomStatus
from app.team.store import ChatMessageNotFoundError


def preflight_continuation(service, task_id, request: ContinueTaskPreflightRequest) -> ContinueTaskPreflight:
    task, room = service._task_room(task_id)
    snapshot = service.tasks.get(task_id)
    if snapshot.revision != request.expected_revision:
        raise TaskStateConflict("task revision changed")
    if task_id in service._runs or task_id in service._cancelling:
        raise TaskStateConflict("task execution or cancellation is still active")
    if task.state is not TaskState.NEEDS_HUMAN or room.status is not RoomStatus.ACTIVE:
        raise TaskStateConflict("continuation preflight requires a paused task and active room")
    try:
        stored = service.rooms.get_message(request.message_id)
    except ChatMessageNotFoundError as exc:
        raise TaskMessageNotFound("human message is not in this task room") from exc
    message = stored.message
    if message.task_id != task.id or message.trace_id != task.trace_id or message.room_id != room.room_id:
        raise TaskMessageNotFound("human message is not in this task room")
    humans = [m for m in room.members if m.role is MemberRole.HUMAN and m.kind is MemberKind.HUMAN]
    targets = [m for m in room.members if m.role is request.target_role and m.kind is MemberKind.AGENT]
    orchestrators = [m for m in room.members if m.role is MemberRole.ORCHESTRATOR and m.kind is MemberKind.SYSTEM]
    if len(humans) != 1 or len(targets) != 1 or len(orchestrators) != 1:
        raise TaskDetailUnavailable("continuation requires unambiguous room identities")
    if (message.sender_id != humans[0].member_id or message.type not in {MessageType.MESSAGE, MessageType.ANSWER}
            or not message.idempotency_key.startswith("human-api:") or message.artifacts):
        raise TaskMessageInvalid("continuation must reference a human API message")
    if len(stored.deliveries) != 1:
        raise TaskMessageInvalid("human message must have exactly one delivery")
    delivery = stored.deliveries[0]
    if delivery.status is not MessageDeliveryStatus.PENDING:
        raise TaskStateConflict("human message was already acknowledged")
    if delivery.recipient_id not in {targets[0].member_id, orchestrators[0].member_id}:
        raise TaskMessageInvalid("target Agent does not match the human message recipient")
    context = service.contexts.get(task_id)
    bindings = [b for b in context.context.agent_bindings if b.role.value == request.target_role.value]
    if len(bindings) != 1 or bindings[0].agent_name != service.agent_names.get(request.target_role):
        raise TaskDetailUnavailable("persisted and configured Agent binding do not match")
    rounds_limit = service.event_loop.controller.max_rework_rounds
    if task.rework_rounds >= rounds_limit:
        raise TaskStateConflict("rework budget exhausted; continuation cannot reset the budget")
    executor = getattr(service.event_loop, "executor", None)
    guard = getattr(executor, "budget_guard", None)
    if not isinstance(guard, ConversationBudgetGuard) or guard.rooms.database.path != service.rooms.database.path:
        raise TaskServiceUnavailable("configured workflow budget guard is unavailable")
    try:
        violation = guard.evaluate(task.id, room_id=room.room_id)
        usage = guard.usage(task.id, room_id=room.room_id)
    except sqlite3.Error as exc:
        raise TaskServiceUnavailable("workflow budget evidence cannot be read") from exc
    if violation is not None:
        raise TaskStateConflict(f"conversation budget blocked continuation: {violation.code.value}")
    return ContinueTaskPreflight(
        task_id=task.id, trace_id=task.trace_id, task_revision=snapshot.revision,
        runtime_revision=context.revision, message_id=message.message_id,
        correlation_id=message.correlation_id, target_role=request.target_role,
        target_member_id=targets[0].member_id, rework_rounds=task.rework_rounds,
        max_rework_rounds=rounds_limit, budget_usage=usage,
    )
