import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.storage import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactType,
    SQLiteDatabase,
)


def make_store(tmp_path: Path) -> ArtifactStore:
    store = ArtifactStore(
        SQLiteDatabase(tmp_path / "codecrew.sqlite3"),
        tmp_path / "artifacts",
    )
    store.initialize()
    return store


def common_fields() -> dict[str, object]:
    return {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "created_by": "planner",
    }


def test_stores_and_reads_text_with_metadata(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    artifact = store.put_text(
        "implementation plan",
        type=ArtifactType.PLAN,
        filename="plan.txt",
        metadata={"attempt": "1"},
        **common_fields(),
    )

    assert store.read_text(artifact.artifact_id) == "implementation plan"
    assert store.get_metadata(artifact.artifact_id) == artifact
    assert artifact.size_bytes == len(b"implementation plan")
    assert store.blob_path_for(artifact.artifact_id).is_file()


def test_json_is_canonical_and_round_trips(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    fields = common_fields()

    first = store.put_json(
        {"b": 2, "a": [True, None]},
        type=ArtifactType.VERIFICATION_REPORT,
        **fields,
    )
    second = store.put_json(
        {"a": [True, None], "b": 2},
        type=ArtifactType.VERIFICATION_REPORT,
        **fields,
    )

    assert first.sha256 == second.sha256
    assert store.read_json(first.artifact_id) == {"a": [True, None], "b": 2}


def test_duplicate_content_uses_one_physical_blob(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    first = store.put_bytes(
        b"same diff",
        type=ArtifactType.DIFF,
        media_type="text/x-diff",
        **common_fields(),
    )
    second = store.put_bytes(
        b"same diff",
        type=ArtifactType.DIFF,
        media_type="text/x-diff",
        **common_fields(),
    )

    assert first.artifact_id != second.artifact_id
    assert first.sha256 == second.sha256
    assert store.blob_path_for(first.artifact_id) == store.blob_path_for(second.artifact_id)
    assert len(list((store.root / "sha256").glob("*/*"))) == 1


def test_streams_large_content_in_chunks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    content = b"0123456789" * 20_000

    artifact = store.put_stream(
        io.BytesIO(content),
        type=ArtifactType.TEST_LOG,
        media_type="text/plain",
        chunk_size=1024,
        **common_fields(),
    )
    chunks = list(store.iter_bytes(artifact.artifact_id, chunk_size=4096))

    assert b"".join(chunks) == content
    assert len(chunks) > 1


def test_metadata_and_content_survive_store_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = store.put_text("persisted", type=ArtifactType.PLAN, **common_fields())

    reopened = ArtifactStore(SQLiteDatabase(store.database.path), store.root)
    reopened.initialize()

    assert reopened.get_metadata(artifact.artifact_id) == artifact
    assert reopened.read_text(artifact.artifact_id) == "persisted"


def test_detects_modified_and_missing_blob(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    modified = store.put_bytes(
        b"original",
        type=ArtifactType.DIFF,
        media_type="text/x-diff",
        **common_fields(),
    )
    store.blob_path_for(modified.artifact_id).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="integrity check failed"):
        store.read_bytes(modified.artifact_id)

    missing = store.put_bytes(
        b"will disappear",
        type=ArtifactType.TEST_LOG,
        media_type="text/plain",
        **common_fields(),
    )
    store.blob_path_for(missing.artifact_id).unlink()
    with pytest.raises(ArtifactIntegrityError, match="blob is missing"):
        store.read_bytes(missing.artifact_id)


def test_unknown_artifact_is_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
        store.get_metadata(UUID(int=0))


def test_rejects_unsafe_filename_and_non_binary_stream(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="basename"):
        store.put_text(
            "unsafe",
            type=ArtifactType.PLAN,
            filename="../plan.txt",
            **common_fields(),
        )

    with pytest.raises(TypeError, match="must return bytes"):
        store.put_stream(
            io.StringIO("not bytes"),  # type: ignore[arg-type]
            type=ArtifactType.GENERIC,
            media_type="text/plain",
            **common_fields(),
        )

    assert list((store.root / ".tmp").iterdir()) == []
    assert not (store.root / "sha256").exists()


def test_reference_uses_persisted_integrity_data(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = store.put_text("plan", type=ArtifactType.PLAN, **common_fields())

    reference = store.get_reference(artifact.artifact_id, summary="Planner output")

    assert reference.artifact_id == artifact.artifact_id
    assert reference.sha256 == artifact.sha256


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_stream_chunk_size_must_be_positive(tmp_path: Path, chunk_size: int) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="positive"):
        store.put_stream(
            io.BytesIO(b"data"),
            type=ArtifactType.GENERIC,
            media_type="application/octet-stream",
            chunk_size=chunk_size,
            **common_fields(),
        )
