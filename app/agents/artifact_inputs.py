"""Recheck trusted Artifact grants without importing the storage implementation."""

import hashlib
from pathlib import Path

from app.agents.base import AgentAdapterError
from app.agents.models import AgentArtifactInput


def verify_artifact_files(inputs: tuple[AgentArtifactInput, ...]) -> tuple[Path, ...]:
    """Stream hashes and reject missing files, directories or symlink redirection.

    Called before launch and before acknowledging a turn. This detects tampering;
    it is not an atomic snapshot against a hostile concurrent filesystem owner.
    """
    paths = []
    for item in inputs:
        path = item.path
        try:
            if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
                raise AgentAdapterError(
                    f"artifact input is not a regular direct file: {item.artifact_id}"
                )
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            if size != item.size_bytes or digest.hexdigest() != item.sha256:
                raise AgentAdapterError(f"artifact input integrity failed: {item.artifact_id}")
        except OSError as exc:
            raise AgentAdapterError(f"artifact input is unreadable: {item.artifact_id}") from exc
        paths.append(path)
    return tuple(dict.fromkeys(paths))
