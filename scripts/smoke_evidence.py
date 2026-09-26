"""Archive a quiescent, disposable smoke fixture; never determine task success."""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import UUID

from app.orchestration.models import Task
from app.storage import ArtifactStore, SQLiteDatabase


def archive_smoke_evidence(store: ArtifactStore, task: Task, *, root: Path) -> Path:
    """Copy only SQLite and registered blobs, not workspaces, CLI homes or env.

    The caller must have stopped Agent turns and database writes. The manifest
    marks archive integrity, not workflow acceptance. Partial directories without
    a manifest remain diagnostic only; they must not be treated as valid archives.
    """
    if root.is_symlink():
        raise ValueError("evidence archive root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    archive = Path(tempfile.mkdtemp(prefix=f"{task.trace_id}-", dir=root))
    database_path = archive / "trace.sqlite3"
    with store.database.connect() as source:
        destination = sqlite3.connect(database_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    database_path.chmod(0o600)
    snapshot = ArtifactStore(SQLiteDatabase(database_path), archive / "artifacts")
    with snapshot.database.connect() as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("evidence database failed integrity check")
        artifact_ids = [UUID(row["artifact_id"]) for row in connection.execute(
            "SELECT artifact_id FROM artifacts ORDER BY created_at, artifact_id"
        )]
    records = []
    for artifact_id in artifact_ids:
        metadata = snapshot.get_metadata(artifact_id)
        if metadata.task_id != task.id or metadata.trace_id != task.trace_id:
            raise ValueError("smoke archive contains another task or trace")
        source_path = store.blob_path_for(artifact_id)
        if source_path.is_symlink():
            raise ValueError("evidence blob must not be a symlink")
        path = snapshot.blob_path_for(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("xb") as output:
                for chunk in store.iter_bytes(artifact_id):
                    output.write(chunk)
            path.chmod(0o600)
        # Verify archived bytes against the backed-up metadata, including deduped blobs.
        for _ in snapshot.iter_bytes(artifact_id):
            pass
        records.append(metadata.model_dump(mode="json"))
    with database_path.open("rb") as database_file:
        database_sha256 = hashlib.file_digest(database_file, "sha256").hexdigest()
    manifest = {
        "schema_version": 1,
        "scope": "three-agent-smoke-evidence",
        "task_id": str(task.id),
        "trace_id": str(task.trace_id),
        "task_state": task.state.value,
        "archive_integrity_verified": True,
        "database": "trace.sqlite3",
        "database_sha256": database_sha256,
        "artifact_root": "artifacts",
        "artifacts": records,
        "task_snapshot": task.model_dump(mode="json"),
        "limitations": [
            "Archive integrity does not establish task success or real model acceptance.",
            "CLI homes, runtime directories and environment variables are not archived.",
            "Recorded Agent replies and logs may contain sensitive data; review before sharing.",
            "Recorded workspace paths may no longer exist; this is not a resumable workspace.",
        ],
    }
    path = archive / "manifest.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    path.chmod(0o600)
    return archive
