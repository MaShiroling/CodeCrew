from pathlib import Path
from uuid import uuid4

import pytest

from app.orchestration.models import Task, TaskState
from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase
from app.team import (
    ChatMessage,
    ConversationRouter,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    TeamRoom,
    TeamRoomStore,
    WorkflowController,
    WorkflowControllerError,
    WorkflowDirectiveKind,
)


def make_context(tmp_path: Path, *, max_rework_rounds: int = 2):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    rooms = TeamRoomStore(database)
    artifacts.initialize()
    rooms.initialize()
    room_id = uuid4()

    def member(name: str, role: MemberRole, kind: MemberKind):
        return RoomMember(room_id=room_id, name=name, role=role, kind=kind)

    members = {
        MemberRole.PLANNER: member("planner", MemberRole.PLANNER, MemberKind.AGENT),
        MemberRole.IMPLEMENTER: member("implementer", MemberRole.IMPLEMENTER, MemberKind.AGENT),
        MemberRole.REVIEWER: member("reviewer", MemberRole.REVIEWER, MemberKind.AGENT),
        MemberRole.VERIFIER: member("verifier", MemberRole.VERIFIER, MemberKind.SYSTEM),
        MemberRole.ORCHESTRATOR: member("orchestrator", MemberRole.ORCHESTRATOR, MemberKind.SYSTEM),
        MemberRole.HUMAN: member("human", MemberRole.HUMAN, MemberKind.HUMAN),
    }
    task = Task(issue="Implement feature", repository_path=str(tmp_path))
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Workflow room",
        members=tuple(members.values()),
    )
    rooms.create_room(room)
    router = ConversationRouter(rooms, artifacts)
    controller = WorkflowController(rooms, max_rework_rounds=max_rework_rounds)
    controller.initialize()
    return controller, router, artifacts, task, room, members


def artifact(
    artifacts: ArtifactStore,
    task: Task,
    type: ArtifactType,
    *,
    review_verdict: str = "rejected",
) -> ArtifactReference:
    content = {"type": type.value}
    if type is ArtifactType.REVIEW_REPORT:
        content = {
            "task_id": str(task.id),
            "trace_id": str(task.trace_id),
            "reviewer": "test-reviewer",
            "verdict": review_verdict,
            "issues": (
                []
                if review_verdict == "approved"
                else [
                    {
                        "issue_id": str(uuid4()),
                        "priority": "high",
                        "summary": "implementation needs correction",
                        "resolved": False,
                    }
                ]
            ),
            "summary": "rework required",
        }
    metadata = artifacts.put_json(
        content,
        task_id=task.id,
        trace_id=task.trace_id,
        type=type,
        created_by="test",
        filename=f"{type.value}.json",
    )
    return ArtifactReference.from_metadata(metadata, summary=type.value)


def emit(router, room, sender, recipient, type, *, artifacts=(), content="event"):
    return router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=room.task_id,
            trace_id=room.trace_id,
            sender_id=sender.member_id,
            recipients=(
                MessageRecipient(
                    kind=RecipientKind.MEMBER,
                    member_id=recipient.member_id,
                ),
            ),
            type=type,
            content=content,
            artifacts=artifacts,
            idempotency_key=f"event-{uuid4()}",
        ),
        authenticated_sender_id=sender.member_id,
    )


def advance_to_reviewing(controller, router, artifacts, task, room, members):
    issue = emit(
        router,
        room,
        members[MemberRole.HUMAN],
        members[MemberRole.PLANNER],
        MessageType.ISSUE_POSTED,
    )
    controller.handle(task, issue)
    plan = emit(
        router,
        room,
        members[MemberRole.PLANNER],
        members[MemberRole.IMPLEMENTER],
        MessageType.PLAN_SHARED,
        artifacts=(artifact(artifacts, task, ArtifactType.PLAN),),
    )
    controller.handle(task, plan)
    ready = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.ORCHESTRATOR],
        MessageType.IMPLEMENTATION_READY,
    )
    controller.handle(task, ready)
    verified = emit(
        router,
        room,
        members[MemberRole.VERIFIER],
        members[MemberRole.REVIEWER],
        MessageType.VERIFICATION_READY,
        artifacts=(artifact(artifacts, task, ArtifactType.VERIFICATION_REPORT),),
    )
    controller.handle(task, verified)


def test_events_drive_happy_path_without_direct_stage_calls(tmp_path: Path) -> None:
    controller, router, artifacts, task, room, members = make_context(tmp_path)
    issue = emit(
        router,
        room,
        members[MemberRole.HUMAN],
        members[MemberRole.PLANNER],
        MessageType.ISSUE_POSTED,
    )
    decision = controller.handle(task, issue)
    assert task.state is TaskState.PLANNING
    assert decision.directives[0].target_role is MemberRole.PLANNER

    plan = emit(
        router,
        room,
        members[MemberRole.PLANNER],
        members[MemberRole.IMPLEMENTER],
        MessageType.PLAN_SHARED,
        artifacts=(artifact(artifacts, task, ArtifactType.PLAN),),
    )
    decision = controller.handle(task, plan)
    assert task.state is TaskState.IMPLEMENTING
    assert decision.directives[0].target_role is MemberRole.IMPLEMENTER

    ready = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.ORCHESTRATOR],
        MessageType.IMPLEMENTATION_READY,
    )
    decision = controller.handle(task, ready)
    assert task.state is TaskState.VERIFYING
    assert decision.directives[0].kind is WorkflowDirectiveKind.RUN_VERIFIER

    verified = emit(
        router,
        room,
        members[MemberRole.VERIFIER],
        members[MemberRole.REVIEWER],
        MessageType.VERIFICATION_READY,
        artifacts=(artifact(artifacts, task, ArtifactType.VERIFICATION_REPORT),),
    )
    decision = controller.handle(task, verified)
    assert task.state is TaskState.REVIEWING
    assert decision.directives[0].target_role is MemberRole.REVIEWER

    approved = emit(
        router,
        room,
        members[MemberRole.REVIEWER],
        members[MemberRole.ORCHESTRATOR],
        MessageType.REVIEW_APPROVED,
        artifacts=(
            artifact(
                artifacts,
                task,
                ArtifactType.REVIEW_REPORT,
                review_verdict="approved",
            ),
        ),
    )
    decision = controller.handle(task, approved)
    assert task.state is TaskState.REVIEWING
    assert decision.directives[0].kind is WorkflowDirectiveKind.RUN_COMPLETION_GUARD

    completed = emit(
        router,
        room,
        members[MemberRole.ORCHESTRATOR],
        members[MemberRole.HUMAN],
        MessageType.COMPLETION_PASSED,
        artifacts=(artifact(artifacts, task, ArtifactType.COMPLETION_DECISION),),
    )
    controller.handle(task, completed)
    assert task.state is TaskState.COMPLETED


def test_question_wakes_recipient_without_changing_state(tmp_path: Path) -> None:
    controller, router, _, task, room, members = make_context(tmp_path)
    task.transition_to(TaskState.PLANNING)
    question = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.PLANNER],
        MessageType.QUESTION,
    )

    decision = controller.handle(task, question)

    assert task.state is TaskState.PLANNING
    assert decision.directives[0].kind is WorkflowDirectiveKind.WAKE_MEMBERS
    assert decision.directives[0].target_member_ids == (members[MemberRole.PLANNER].member_id,)


def test_queued_event_remains_valid_after_delivery_ack(tmp_path: Path) -> None:
    controller, router, _, task, room, members = make_context(tmp_path)
    question = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.PLANNER],
        MessageType.QUESTION,
    )
    controller.rooms.acknowledge(
        question.message.message_id, recipient_id=members[MemberRole.PLANNER].member_id
    )
    assert controller.rooms.get_message(question.message.message_id) != question
    decision = controller.handle(task, question)
    assert decision.directives[0].target_member_ids == (members[MemberRole.PLANNER].member_id,)
    assert controller.handle(task, question).replayed


@pytest.mark.parametrize("changed", ["content", "sequence", "recipient"])
def test_delivery_ack_does_not_allow_forged_event(tmp_path: Path, changed: str) -> None:
    controller, router, _, task, room, members = make_context(tmp_path)
    question = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.PLANNER],
        MessageType.QUESTION,
    )
    if changed == "content":
        forged = question.model_copy(
            update={"message": question.message.model_copy(update={"content": "forged"})}
        )
    elif changed == "sequence":
        forged = question.model_copy(update={"sequence": question.sequence + 1})
    else:
        forged = question.model_copy(
            update={
                "deliveries": (
                    question.deliveries[0].model_copy(
                        update={"recipient_id": members[MemberRole.REVIEWER].member_id}
                    ),
                )
            }
        )
    with pytest.raises(WorkflowControllerError, match="does not match persisted"):
        controller.handle(task, forged)
    assert task.state is TaskState.CREATED


def test_revised_plan_wakes_implementer_without_restarting_state(tmp_path: Path) -> None:
    controller, router, artifacts, task, room, members = make_context(tmp_path)
    task.transition_to(TaskState.PLANNING)
    initial = emit(
        router,
        room,
        members[MemberRole.PLANNER],
        members[MemberRole.IMPLEMENTER],
        MessageType.PLAN_SHARED,
        artifacts=(artifact(artifacts, task, ArtifactType.PLAN),),
    )
    controller.handle(task, initial)
    question = emit(
        router,
        room,
        members[MemberRole.IMPLEMENTER],
        members[MemberRole.PLANNER],
        MessageType.QUESTION,
    )
    revised_artifact = artifact(artifacts, task, ArtifactType.PLAN)
    revised = router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=members[MemberRole.PLANNER].member_id,
            recipients=(MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.IMPLEMENTER),),
            type=MessageType.PLAN_SHARED,
            content="Clarified implementation plan",
            artifacts=(revised_artifact,),
            supersedes_artifact_id=initial.message.artifacts[0].artifact_id,
            addresses_message_ids=(question.message.message_id,),
            idempotency_key=f"event-{uuid4()}",
        ),
        authenticated_sender_id=members[MemberRole.PLANNER].member_id,
    )

    decision = controller.handle(task, revised)

    assert task.state is TaskState.IMPLEMENTING
    assert decision.transitions == ()
    assert decision.directives[0].target_role is MemberRole.IMPLEMENTER
    assert decision.directives[0].reason == "revised plan is available"


def test_reprocessing_event_is_durable_and_idempotent(tmp_path: Path) -> None:
    controller, router, _, task, room, members = make_context(tmp_path)
    issue = emit(
        router,
        room,
        members[MemberRole.HUMAN],
        members[MemberRole.PLANNER],
        MessageType.ISSUE_POSTED,
    )
    first = controller.handle(task, issue)

    repeated = controller.handle(task, issue)
    recovered_task = Task(
        id=task.id,
        trace_id=task.trace_id,
        issue=task.issue,
        repository_path=task.repository_path,
    )
    recovered = controller.handle(recovered_task, issue)

    assert not first.replayed
    assert repeated.replayed
    assert recovered.replayed
    assert recovered_task.state is TaskState.PLANNING


def test_illegal_event_order_is_rejected_without_state_change(tmp_path: Path) -> None:
    controller, router, artifacts, task, room, members = make_context(tmp_path)
    plan = emit(
        router,
        room,
        members[MemberRole.PLANNER],
        members[MemberRole.IMPLEMENTER],
        MessageType.PLAN_SHARED,
        artifacts=(artifact(artifacts, task, ArtifactType.PLAN),),
    )

    with pytest.raises(WorkflowControllerError, match="requires task state planning"):
        controller.handle(task, plan)

    assert task.state is TaskState.CREATED


def test_rework_budget_exhaustion_routes_to_human(tmp_path: Path) -> None:
    controller, router, artifacts, task, room, members = make_context(tmp_path, max_rework_rounds=0)
    advance_to_reviewing(controller, router, artifacts, task, room, members)
    rejected = emit(
        router,
        room,
        members[MemberRole.REVIEWER],
        members[MemberRole.ORCHESTRATOR],
        MessageType.REWORK_REQUEST,
        artifacts=(artifact(artifacts, task, ArtifactType.REVIEW_REPORT),),
    )

    decision = controller.handle(task, rejected)

    assert task.state is TaskState.NEEDS_HUMAN
    assert task.rework_rounds == 0
    assert decision.transitions == (TaskState.REWORK, TaskState.NEEDS_HUMAN)
    assert decision.directives[0].kind is WorkflowDirectiveKind.REQUEST_HUMAN


def test_rework_event_starts_next_implementation_round(tmp_path: Path) -> None:
    controller, router, artifacts, task, room, members = make_context(tmp_path)
    advance_to_reviewing(controller, router, artifacts, task, room, members)
    rejected = emit(
        router,
        room,
        members[MemberRole.REVIEWER],
        members[MemberRole.ORCHESTRATOR],
        MessageType.REWORK_REQUEST,
        artifacts=(artifact(artifacts, task, ArtifactType.REVIEW_REPORT),),
    )

    decision = controller.handle(task, rejected)

    assert task.state is TaskState.IMPLEMENTING
    assert task.rework_rounds == 1
    assert decision.transitions == (TaskState.REWORK, TaskState.IMPLEMENTING)
    assert decision.directives[0].kind is WorkflowDirectiveKind.WAKE_AGENT
    assert decision.directives[0].target_role is MemberRole.IMPLEMENTER
