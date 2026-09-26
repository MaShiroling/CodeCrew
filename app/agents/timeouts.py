"""Shared bounds for explicitly configured Planner turn deadlines."""

MAX_PLANNER_TIMEOUT_SECONDS = 900


def validate_planner_timeout(value: int | None) -> int | None:
    if value is not None and (
        type(value) is not int or not 1 <= value <= MAX_PLANNER_TIMEOUT_SECONDS
    ):
        raise ValueError("planner_timeout_seconds must be an integer between 1 and 900")
    return value
