"""Validate the independently reviewed, frozen memory generalization set."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from evals.validate_memory_baseline import load_cases, validate_cases

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "memory-generalization-v1.jsonl"
LOCK_PATH = ROOT / "memory-generalization-v1.lock.json"
PRIOR_PATHS = (
    ROOT / "memory-baseline-v1.jsonl",
    ROOT / "memory-unseen-v1.jsonl",
)


def validate_candidate(path: Path = CASES_PATH) -> list[dict]:
    """Check structure, label grounding, coverage, and exact-text disjointness."""
    cases = load_cases(path)
    validate_cases(cases)
    if len(cases) != 12:
        raise ValueError("generalization candidate must contain 12 cases")
    domains = Counter(case["domain"] for case in cases)
    if domains != {"development": 6, "personal": 6}:
        raise ValueError("generalization candidate must have six cases per domain")

    prior = [case for old_path in PRIOR_PATHS for case in load_cases(old_path)]
    prior_ids = {case["id"] for case in prior}
    prior_texts = {
        text
        for case in prior
        for text in (
            *(message["content"] for message in case["messages"]),
            case["query"]["content"],
        )
    }
    if prior_ids.intersection(case["id"] for case in cases):
        raise ValueError("generalization candidate reuses a prior case id")
    new_texts = [
        text
        for case in cases
        for text in (
            *(message["content"] for message in case["messages"]),
            case["query"]["content"],
        )
    ]
    if prior_texts.intersection(new_texts) or len(new_texts) != len(set(new_texts)):
        raise ValueError("generalization candidate reuses source or query text")

    for domain in domains:
        group = [case for case in cases if case["domain"] == domain]
        if not any(not case["gold"]["writes"] for case in group):
            raise ValueError(f"{domain} needs a no-write case")
        if not any(
            case["gold"]["active_sources"] and not case["gold"]["recall_sources"]
            for case in group
        ):
            raise ValueError(f"{domain} needs a scoped no-recall case")

    tags = Counter(tag for case in cases for tag in case["tags"])
    for tag, minimum in (("prohibition", 2), ("room_boundary", 2), ("format", 2)):
        if tags[tag] < minimum:
            raise ValueError(f"generalization candidate needs {minimum} {tag} cases")

    for case in cases:
        by_id = {message["id"]: message for message in case["messages"]}
        for write in case["gold"]["writes"]:
            if write["fact"] not in by_id[write["source"]]["content"]:
                raise ValueError(f"non-extractive gold fact in {case['id']}")
    return cases


def validate_frozen(path: Path = CASES_PATH, lock_path: Path = LOCK_PATH) -> list[dict]:
    """Require user-reviewed labels and unchanged dataset bytes before evaluation."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("version") != 1 or lock.get("dataset") != path.name:
        raise ValueError("unsupported memory generalization lock")
    if lock.get("label_provenance") != "independently_human_reviewed":
        raise ValueError("memory generalization labels need independent human review")
    if lock.get("reviewer") != "user" or not lock.get("reviewed_at"):
        raise ValueError("memory generalization review record is incomplete")
    if hashlib.sha256(path.read_bytes()).hexdigest() != lock.get("sha256"):
        raise ValueError("memory generalization dataset changed after freeze")
    cases = validate_candidate(path)
    if len(cases) != lock.get("cases"):
        raise ValueError("memory generalization lock case count disagrees")
    domains = Counter(case["domain"] for case in cases)
    if domains != {
        "development": lock.get("development"),
        "personal": lock.get("personal"),
    }:
        raise ValueError("memory generalization lock domain counts disagree")
    return cases


def main() -> None:
    cases = validate_frozen()
    print(f"Validated frozen generalization set: {len(cases)} cases (6 + 6)")


if __name__ == "__main__":
    main()
