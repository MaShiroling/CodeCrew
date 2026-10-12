"""The first memory quality fixture is stable, balanced, and internally consistent."""

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from app.chat.memory import AgentMemoryProposal, _auto_preference_has_user_source
from evals.validate_memory_baseline import (
    load_cases,
    load_splits,
    validate_cases,
    validate_splits,
)


def test_memory_baseline_v1_is_balanced_and_valid() -> None:
    cases = load_cases()
    validate_cases(cases)
    validate_splits(cases, load_splits())
    assert len(cases) == 20
    assert Counter(case["domain"] for case in cases) == {
        "development": 10, "personal": 10,
    }
    tags = {tag for case in cases for tag in case["tags"]}
    assert {
        "paraphrase", "correction", "no_write", "room_boundary",
        "external_boundary", "sensitive", "same_discussion",
    } <= tags
    assert any(case["gold"]["writes"] for case in cases)
    assert any(not case["gold"]["writes"] for case in cases)


def test_memory_baseline_rejects_bad_gold_scope() -> None:
    cases = load_cases()
    cases[0]["gold"]["recall_sources"] = ["missing"]
    with pytest.raises(ValueError, match="recall label"):
        validate_cases(cases)


def test_memory_baseline_rejects_split_overlap() -> None:
    cases = load_cases()
    splits = load_splits()
    splits["holdout"][0] = splits["tuning"][0]
    with pytest.raises(ValueError, match="partition"):
        validate_splits(cases, splits)


def test_frozen_kimi_baseline_matches_dataset_and_reported_totals() -> None:
    root = Path(__file__).resolve().parents[1] / "evals"
    baseline = json.loads(
        (root / "baselines" / "kimi-current-v1.json").read_text(encoding="utf-8")
    )
    cases = load_cases()
    assert baseline["dataset_sha256"] == hashlib.sha256(
        (root / "memory-baseline-v1.jsonl").read_bytes()
    ).hexdigest()
    assert set(baseline["case_counts"]) == {case["id"] for case in cases}
    assert baseline["metric_order"] == ["tp", "fp", "fn"]
    assert [sum(counts["write"][index] for counts in baseline["case_counts"].values())
            for index in range(3)] == [16, 1, 0]
    assert [sum(counts["recall"][index] for counts in baseline["case_counts"].values())
            for index in range(3)] == [9, 0, 2]
    assert baseline["answer_review"]["human_confirmed"] is False


def test_attribution_guard_preserves_labeled_writes() -> None:
    for case in load_cases():
        messages = {message["id"]: message for message in case["messages"]}
        for write in case["gold"]["writes"]:
            proposal = AgentMemoryProposal(kind=write["kind"], content=write["fact"])
            assert _auto_preference_has_user_source(
                messages[write["source"]]["content"], proposal,
            ), case["id"]
