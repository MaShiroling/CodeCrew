import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import AgentAdapterError, AgentArtifactInput
from app.agents.artifact_inputs import verify_artifact_files


def grant(path: Path, content: bytes) -> AgentArtifactInput:
    return AgentArtifactInput(
        artifact_id=uuid4(),
        task_id=uuid4(),
        trace_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def test_verifies_large_input_with_streaming_hash(tmp_path: Path) -> None:
    content = b"x" * (128 * 1024 + 3)
    path = tmp_path / "plan"
    path.write_bytes(content)
    assert verify_artifact_files((grant(path, content),)) == (path,)


@pytest.mark.parametrize(
    "mutation", ["missing", "directory", "symlink", "parent-symlink", "hash", "size"]
)
def test_rejects_invalid_grants(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "plan"
    content = b"plan"
    path.write_bytes(content)
    item = grant(path, content)
    if mutation == "missing":
        path.unlink()
    elif mutation == "directory":
        path.unlink()
        path.mkdir()
    elif mutation == "symlink":
        target = tmp_path / "other"
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "parent-symlink":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        item = item.model_copy(update={"path": link / "plan"})
    elif mutation == "hash":
        path.write_bytes(b"evil")  # Same size, different content.
    else:
        item = item.model_copy(update={"size_bytes": 100})
    with pytest.raises(AgentAdapterError):
        verify_artifact_files((item,))
