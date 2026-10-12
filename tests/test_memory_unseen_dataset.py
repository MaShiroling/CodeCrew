"""Keep the new memory challenge set fixed and distinct from the old pilot."""

import json
from pathlib import Path

import pytest

from evals.validate_memory_unseen import LOCK_PATH, validate_unseen


def test_unseen_v1_is_frozen_and_balanced() -> None:
    cases = validate_unseen()
    assert len(cases) == 24
    assert {case["domain"] for case in cases} == {"development", "personal"}
    assert any("correction" in case["tags"] for case in cases)
    assert any("sensitive" in case["tags"] for case in cases)
    assert any("external_boundary" in case["tags"] for case in cases)


def test_unseen_v1_detects_dataset_mutation(tmp_path: Path) -> None:
    original = Path(__file__).resolve().parents[1] / "evals" / "memory-unseen-v1.jsonl"
    changed = tmp_path / original.name
    changed.write_bytes(original.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed after freeze"):
        validate_unseen(changed)


def test_unseen_v1_human_review_is_recorded() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["label_provenance"] == "independently_human_reviewed"
    assert lock["reviewer"] == "user"
    assert lock["reviewed_at"]
