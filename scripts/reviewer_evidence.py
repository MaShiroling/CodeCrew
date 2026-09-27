"""Shared smoke assertions; observed tool reads are not OS-level isolation."""

from pathlib import Path

from app.agents import AgentEventType
from app.team import AgentTurnResult, WorkflowExecutionError


def check_reviewer_evidence(
    turn: AgentTurnResult,
    *,
    working_directory: Path,
    required_paths: set[Path],
    native_sessions: set[str],
    native_output: bool,
) -> None:
    native_id = turn.session.native_session_id
    if not native_id:
        raise WorkflowExecutionError("Reviewer returned no native session identifier")
    if native_id in native_sessions:
        raise WorkflowExecutionError("Reviewer reused a prior native session")
    paths = set()
    for event in turn.events:
        if event.type is not AgentEventType.TOOL_CALL:
            continue
        call = event.data
        formatter = (
            native_output
            and call.get("name") == "StructuredOutput"
            and call.get("input") == turn.agent_result.output.get("structured_output")
        )
        if call.get("name") not in {"Read", "Glob", "Grep"} and not formatter:
            raise WorkflowExecutionError("Reviewer attempted an unapproved tool")
        args = call.get("input")
        path = args.get("file_path", args.get("path")) if isinstance(args, dict) else None
        if call.get("name") == "Read" and isinstance(path, str):
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = working_directory / candidate
            paths.add(candidate.resolve())
    if not required_paths or not {path.resolve() for path in required_paths} <= paths:
        raise WorkflowExecutionError(
            "Reviewer did not visibly read every supplied evidence Artifact"
        )
    native_sessions.add(native_id)
