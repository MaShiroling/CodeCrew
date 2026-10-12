"""Checks for the frozen memory generalization cases."""

import json
from pathlib import Path

import pytest

from evals.validate_memory_generalization import (
    CASES_PATH,
    LOCK_PATH,
    validate_candidate,
    validate_frozen,
)


def test_candidate_is_balanced_and_valid() -> None:
    cases = validate_frozen()
    assert len(cases) == 12
    assert sum(case["domain"] == "development" for case in cases) == 6
    assert sum(case["domain"] == "personal" for case in cases) == 6


def test_frozen_labels_record_user_review() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["label_provenance"] == "independently_human_reviewed"
    assert lock["reviewer"] == "user"
    assert lock["reviewed_at"]


def test_frozen_dataset_rejects_mutation(tmp_path: Path) -> None:
    changed = tmp_path / CASES_PATH.name
    changed.write_bytes(CASES_PATH.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed after freeze"):
        validate_frozen(changed)


def test_candidate_rejects_prior_text(tmp_path: Path) -> None:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    cases[0]["query"]["content"] = "@月见 网关等待后端响应最多多久？"
    changed = tmp_path / "candidate.jsonl"
    changed.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reuses source or query text"):
        validate_candidate(changed)


def test_candidate_rejects_unfaithful_fact(tmp_path: Path) -> None:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    cases[0]["gold"]["writes"][0]["fact"] = "正式环境可以清空用户缓存。"
    changed = tmp_path / "candidate.jsonl"
    changed.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-extractive gold fact"):
        validate_candidate(changed)
