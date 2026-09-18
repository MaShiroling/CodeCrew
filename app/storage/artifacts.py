import hashlib
import io
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from pydantic import JsonValue

from app.storage.models import ArtifactMetadata, ArtifactReference, ArtifactType
from app.storage.sqlite import Migration, SQLiteDatabase


class ArtifactStoreError(RuntimeError):
    """Base error for artifact persistence failures."""


class ArtifactNotFoundError(ArtifactStoreError):
    pass


class ArtifactIntegrityError(ArtifactStoreError):
    pass


ARTIFACT_STORE_MIGRATIONS = (
    Migration(
        version=1,
        name="create_artifacts",
        statements=(
            """
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                filename TEXT,
                metadata_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX artifacts_task_id_idx ON artifacts(task_id, created_at)",
            "CREATE INDEX artifacts_trace_id_idx ON artifacts(trace_id, created_at)",
            "CREATE INDEX artifacts_sha256_idx ON artifacts(sha256)",
        ),
    ),
)


class ArtifactStore:
    """Immutable metadata records backed by content-addressed files."""

    def __init__(self, database: SQLiteDatabase, root: Path) -> None:
        self.database = database
        self.root = root
        self._blob_root = root / "sha256"
        self._temporary_root = root / ".tmp"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self.database.initialize(ARTIFACT_STORE_MIGRATIONS)

    def put_bytes(
        self,
        content: bytes,
        *,
        task_id: UUID,
        trace_id: UUID,
        type: ArtifactType,
        media_type: str,
        created_by: str,
        filename: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactMetadata:
        return self.put_stream(
            io.BytesIO(content),
            task_id=task_id,
            trace_id=trace_id,
            type=type,
            media_type=media_type,
            created_by=created_by,
            filename=filename,
            metadata=metadata,
        )

    def put_text(
        self,
        content: str,
        *,
        task_id: UUID,
        trace_id: UUID,
        type: ArtifactType,
        created_by: str,
        media_type: str = "text/plain; charset=utf-8",
        filename: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactMetadata:
        return self.put_bytes(
            content.encode("utf-8"),
            task_id=task_id,
            trace_id=trace_id,
            type=type,
            media_type=media_type,
            created_by=created_by,
            filename=filename,
            metadata=metadata,
        )

    def put_json(
        self,
        content: JsonValue,
        *,
        task_id: UUID,
        trace_id: UUID,
        type: ArtifactType,
        created_by: str,
        filename: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactMetadata:
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.put_bytes(
            encoded,
            task_id=task_id,
            trace_id=trace_id,
            type=type,
            media_type="application/json",
            created_by=created_by,
            filename=filename,
            metadata=metadata,
        )

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        task_id: UUID,
        trace_id: UUID,
        type: ArtifactType,
        media_type: str,
        created_by: str,
        filename: str | None = None,
        metadata: Mapping[str, str] | None = None,
        chunk_size: int = 64 * 1024,
    ) -> ArtifactMetadata:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size_bytes = 0
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._temporary_root,
                prefix="artifact-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := stream.read(chunk_size):
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact stream must return bytes")
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

            sha256 = digest.hexdigest()
            artifact = ArtifactMetadata(
                task_id=task_id,
                trace_id=trace_id,
                type=type,
                media_type=media_type,
                sha256=sha256,
                size_bytes=size_bytes,
                created_by=created_by,
                filename=filename,
                metadata=dict(metadata or {}),
            )
            blob_path = self._blob_path(sha256)
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if blob_path.exists():
                temporary_path.unlink()
            else:
                os.replace(temporary_path, blob_path)
            temporary_path = None

            self._insert_metadata(artifact)
            return artifact
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def get_metadata(self, artifact_id: UUID) -> ArtifactMetadata:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (str(artifact_id),),
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return ArtifactMetadata(
            artifact_id=row["artifact_id"],
            task_id=row["task_id"],
            trace_id=row["trace_id"],
            type=row["artifact_type"],
            media_type=row["media_type"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            filename=row["filename"],
            metadata=json.loads(row["metadata_json"]),
        )

    def get_reference(self, artifact_id: UUID, *, summary: str) -> ArtifactReference:
        return ArtifactReference.from_metadata(self.get_metadata(artifact_id), summary=summary)

    def read_bytes(self, artifact_id: UUID) -> bytes:
        artifact = self.get_metadata(artifact_id)
        content = self._read_blob(artifact)
        return content

    def read_text(self, artifact_id: UUID, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(artifact_id).decode(encoding)

    def read_json(self, artifact_id: UUID) -> JsonValue:
        return json.loads(self.read_text(artifact_id))

    def iter_bytes(self, artifact_id: UUID, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        artifact = self.get_metadata(artifact_id)
        blob_path = self._blob_path(artifact.sha256)
        if not blob_path.is_file():
            raise ArtifactIntegrityError(f"artifact blob is missing: {artifact.artifact_id}")

        digest = hashlib.sha256()
        size_bytes = 0
        with blob_path.open("rb") as blob:
            while chunk := blob.read(chunk_size):
                digest.update(chunk)
                size_bytes += len(chunk)
                yield chunk
        self._verify_integrity(artifact, digest.hexdigest(), size_bytes)

    def blob_path_for(self, artifact_id: UUID) -> Path:
        artifact = self.get_metadata(artifact_id)
        return self._blob_path(artifact.sha256)

    def _insert_metadata(self, artifact: ArtifactMetadata) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, task_id, trace_id, artifact_type, media_type,
                    sha256, size_bytes, created_by, created_at, filename, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(artifact.artifact_id),
                    str(artifact.task_id),
                    str(artifact.trace_id),
                    artifact.type.value,
                    artifact.media_type,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.created_by,
                    artifact.created_at.isoformat(),
                    artifact.filename,
                    json.dumps(artifact.metadata, sort_keys=True, separators=(",", ":")),
                ),
            )

    def _read_blob(self, artifact: ArtifactMetadata) -> bytes:
        blob_path = self._blob_path(artifact.sha256)
        try:
            content = blob_path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError(
                f"artifact blob is missing: {artifact.artifact_id}"
            ) from exc
        self._verify_integrity(
            artifact,
            hashlib.sha256(content).hexdigest(),
            len(content),
        )
        return content

    @staticmethod
    def _verify_integrity(artifact: ArtifactMetadata, sha256: str, size_bytes: int) -> None:
        if size_bytes != artifact.size_bytes or sha256 != artifact.sha256:
            raise ArtifactIntegrityError(f"artifact integrity check failed: {artifact.artifact_id}")

    def _blob_path(self, sha256: str) -> Path:
        return self._blob_root / sha256[:2] / sha256
