"""Read-only Reviewer contract replay; never execute a workflow or call models."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import UUID

from app.agents import AgentExitReason, AgentResult
from app.storage import ArtifactMetadata, ArtifactStore, SQLiteDatabase
from app.structured_output import parse_json_response
from app.team.actions import ChatActionError, parse_agent_chat_turn
from app.team.reviewer_contract import reviewer_turn_schema
from app.team.store import TeamRoomStore
from app.trace import TraceEvent, TraceEventType

DEFAULT_FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/reviewer_replay.json"


class ReplayInputError(ValueError):
    """Missing, malformed or inconsistent input is not a replay result."""


class _ReadOnlyDatabase(SQLiteDatabase):
    def _open_connection(self):
        # Archived snapshots must be quiescent. Never run migrations or enable
        # WAL: even a normal ArtifactStore read would otherwise create sidecars.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection


def replay_output(output: dict, *, source: str, members=()) -> dict:
    if source not in {"text", "native"} or not isinstance(output, dict):
        raise ReplayInputError("invalid output source")
    try:
        turn = parse_agent_chat_turn(
            output,
            require_structured_output=source == "native",
            output_schema=reviewer_turn_schema(members),
        )
    except ChatActionError as error:
        # Never serialize the exception/instance: it may contain private prose.
        result = {"decision": "rejected", "reason": "contract_rejected"}
        if isinstance(error.__cause__, json.JSONDecodeError):
            cause = error.__cause__
            result.update(
                reason="invalid_json", line=cause.lineno, column=cause.colno, offset=cause.pos
            )
        elif source == "native" and not isinstance(output.get("structured_output"), dict):
            result["reason"] = "native_output_missing"
        return result
    return {
        "decision": "accepted",
        "reason": "valid_contract",
        "action_types": [action.action.value for action in turn.actions],
    }


def replay_fixtures(path: Path = DEFAULT_FIXTURES) -> dict:
    corpus = parse_json_response(path.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
        raise ReplayInputError("unsupported fixture format")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReplayInputError("fixture cases required")
    results = []
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ReplayInputError("invalid fixture case")
        if case["case_id"] in seen:
            raise ReplayInputError("duplicate fixture case")
        seen.add(case["case_id"])
        expected = case.get("expected")
        if not isinstance(expected, dict) or expected.get("decision") not in {
            "accepted",
            "rejected",
        }:
            raise ReplayInputError("fixture expectation required")
        result = replay_output(case["output"], source=case["source"])
        results.append(
            {
                "case_id": case["case_id"],
                "source": case["source"],
                **result,
                "expectation_met": all(result.get(key) == value for key, value in expected.items()),
            }
        )
    return {
        "scope": "reviewer-contract-replay",
        "mode": "fixtures",
        "models_called": False,
        "workflow_executed": False,
        "replay_passed": all(item["expectation_met"] for item in results),
        "cases": results,
    }


def _contained_file(root: Path, relative: Path) -> Path:
    current = root
    for component in relative.parts:
        if component in {"..", ""} or relative.is_absolute():
            raise ReplayInputError("invalid archive path")
        current = current / component
        if current.is_symlink():
            raise ReplayInputError("archive symlinks are not allowed")
    if not current.is_file():
        raise ReplayInputError("archive file missing")
    return current


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_sidecars(root: Path) -> None:
    # Earlier diagnostic readers may have left empty WAL and SHM files. They
    # carry no committed WAL frames; immutable reads ignore them without cleanup.
    for suffix in ("-wal", "-shm", "-journal"):
        path = root / ("trace.sqlite3" + suffix)
        if path.is_symlink():
            raise ReplayInputError("archive sidecar symlink")
        if path.exists() and (not path.is_file() or (suffix != "-shm" and path.stat().st_size)):
            raise ReplayInputError("archive has nonempty transaction sidecars")


def replay_archive(root: Path, *, source: str) -> dict:
    if source not in {"text", "native"} or root.is_symlink() or not root.is_dir():
        raise ReplayInputError("invalid archive or output source")
    root = root.resolve()
    manifest = parse_json_response(
        _contained_file(root, Path("manifest.json")).read_text(encoding="utf-8")
    )
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ReplayInputError("unsupported archive format")
    if (
        manifest.get("scope") != "three-agent-smoke-evidence"
        or manifest.get("database") != "trace.sqlite3"
        or manifest.get("artifact_root") != "artifacts"
    ):
        raise ReplayInputError("invalid archive layout")
    task_id, trace_id = UUID(manifest["task_id"]), UUID(manifest["trace_id"])
    database_path = _contained_file(root, Path("trace.sqlite3"))
    _check_sidecars(root)
    if _digest(database_path) != manifest.get("database_sha256"):
        raise ReplayInputError("archive database checksum mismatch")
    database = _ReadOnlyDatabase(database_path)
    store = ArtifactStore(database, root / "artifacts")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ReplayInputError("artifact manifest required")
    registered = {}
    for record in records:
        metadata = ArtifactMetadata.model_validate(record)
        if (
            metadata.artifact_id in registered
            or metadata.task_id != task_id
            or metadata.trace_id != trace_id
        ):
            raise ReplayInputError("artifact identity mismatch")
        if metadata.model_dump(mode="json") != store.get_metadata(metadata.artifact_id).model_dump(
            mode="json"
        ):
            raise ReplayInputError("artifact manifest differs from database")
        _contained_file(root, Path("artifacts/sha256") / metadata.sha256[:2] / metadata.sha256)
        for _ in store.iter_bytes(metadata.artifact_id):
            pass
        registered[metadata.artifact_id] = metadata
    with database.connect() as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ReplayInputError("invalid archive database")
        ids = {UUID(row[0]) for row in connection.execute("SELECT artifact_id FROM artifacts")}
        if ids != set(registered):
            raise ReplayInputError("artifact manifest is incomplete")
        rooms = connection.execute(
            "SELECT room_id FROM team_rooms WHERE task_id=? AND trace_id=?",
            (str(task_id), str(trace_id)),
        ).fetchall()
        if len(rooms) != 1:
            raise ReplayInputError("exactly one archived task room required")
        rows = connection.execute(
            "SELECT event_json FROM trace_events WHERE event_type=? ORDER BY sequence",
            (TraceEventType.AGENT_OUTPUT_RECORDED.value,),
        ).fetchall()
    members = TeamRoomStore(database).get_room(UUID(rooms[0][0])).members
    results = []
    for row in rows:
        event = TraceEvent.model_validate_json(row[0])
        if (
            event.task_id != task_id
            or event.trace_id != trace_id
            or event.type is not TraceEventType.AGENT_OUTPUT_RECORDED
        ):
            raise ReplayInputError("output event identity mismatch")
        if event.payload.get("role") != "reviewer":
            continue
        artifact_id = UUID(event.payload["artifact_id"])
        metadata = registered.get(artifact_id)
        if metadata is None or metadata.sha256 != event.payload.get("sha256"):
            raise ReplayInputError("output event artifact mismatch")
        recorded = AgentResult.model_validate(store.read_json(artifact_id))
        if recorded.trace_id != trace_id or str(recorded.session_id) != event.payload.get(
            "session_id"
        ):
            raise ReplayInputError("output session identity mismatch")
        result = (
            replay_output(recorded.output, source=source, members=members)
            if recorded.reason is AgentExitReason.COMPLETED and recorded.exit_code in {0, None}
            else {"decision": "rejected", "reason": "agent_failed"}
        )
        results.append(
            {"artifact_id": str(artifact_id), "sha256": metadata.sha256, "source": source, **result}
        )
    if not results:
        raise ReplayInputError("archive contains no recorded Reviewer output")
    # Check again after reads. Never bless a concurrently modified archive.
    if _digest(database_path) != manifest["database_sha256"]:
        raise ReplayInputError("archive changed during replay")
    _check_sidecars(root)
    return {
        "scope": "reviewer-contract-replay",
        "mode": "archive",
        "trace_id": str(trace_id),
        "models_called": False,
        "workflow_executed": False,
        "archive_integrity_verified": True,
        "artifact_count": len(registered),
        "cases": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--fixtures", type=Path, help="portable regression corpus (default)")
    inputs.add_argument("--archive", type=Path, help="read-only smoke evidence directory")
    parser.add_argument(
        "--source", choices=("text", "native"), help="required for archives; never infer a fallback"
    )
    args = parser.parse_args(argv)
    if args.archive is not None and args.source is None:
        parser.error("--archive requires explicit --source")
    if args.archive is None and args.source is not None:
        parser.error("--source is only used with --archive")
    try:
        report = (
            replay_archive(args.archive, source=args.source)
            if args.archive
            else replay_fixtures(args.fixtures or DEFAULT_FIXTURES)
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as error:
        print(
            json.dumps(
                {
                    "scope": "reviewer-contract-replay",
                    "input_verified": False,
                    "error_type": type(error).__name__,
                    "models_called": False,
                    "workflow_executed": False,
                }
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("replay_passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
