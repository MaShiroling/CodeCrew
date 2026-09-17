import pytest

from app.orchestration.models import InvalidTaskTransition, Task, TaskState


def make_task() -> Task:
    return Task(issue="Fix the parser", repository_path="/tmp/example")


def test_happy_path_reaches_completed() -> None:
    task = make_task()

    for state in (
        TaskState.PLANNING,
        TaskState.IMPLEMENTING,
        TaskState.VERIFYING,
        TaskState.REVIEWING,
        TaskState.COMPLETED,
    ):
        task.transition_to(state)

    assert task.is_terminal
    assert task.state is TaskState.COMPLETED


def test_rework_returns_to_implementation() -> None:
    task = make_task()
    for state in (
        TaskState.PLANNING,
        TaskState.IMPLEMENTING,
        TaskState.VERIFYING,
        TaskState.REVIEWING,
        TaskState.REWORK,
        TaskState.IMPLEMENTING,
    ):
        task.transition_to(state)

    assert task.state is TaskState.IMPLEMENTING


def test_invalid_transition_is_rejected() -> None:
    task = make_task()

    with pytest.raises(InvalidTaskTransition, match="created.*completed"):
        task.transition_to(TaskState.COMPLETED)

