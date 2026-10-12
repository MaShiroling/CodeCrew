"""Validate and integrity-check the new, not-yet-run memory challenge set."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from evals.validate_memory_baseline import load_cases, validate_cases

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "memory-unseen-v1.jsonl"
LOCK_PATH = ROOT / "memory-unseen-v1.lock.json"
OLD_CASES_PATH = ROOT / "memory-baseline-v1.jsonl"


def validate_unseen(path: Path = CASES_PATH, lock_path: Path = LOCK_PATH) -> list[dict]:
    """Check schema, labels, coverage, old-case disjointness and frozen bytes."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("version") != 1 or lock.get("dataset") != path.name:
        raise ValueError("unsupported memory unseen lock")
    if hashlib.sha256(path.read_bytes()).hexdigest() != lock.get("sha256"):
        raise ValueError("memory unseen dataset changed after freeze")
    cases = load_cases(path)
    validate_cases(cases)
    if len(cases) != lock.get("cases") or len(cases) != 24:
        raise ValueError("memory unseen dataset must contain 24 cases")
    domains = Counter(case["domain"] for case in cases)
    if domains != {"development": lock.get("development"), "personal": lock.get("personal")}:
        raise ValueError("memory unseen dataset must balance both domains")
    if domains != {"development": 12, "personal": 12}:
        raise ValueError("memory unseen dataset must contain 12 cases per domain")

    old_cases = load_cases(OLD_CASES_PATH)
    old_ids = {case["id"] for case in old_cases}
    old_texts = {message["content"] for case in old_cases for message in case["messages"]}
    new_texts = [message["content"] for case in cases for message in case["messages"]]
    if old_ids.intersection(case["id"] for case in cases):
        raise ValueError("memory unseen case reuses a baseline id")
    if old_texts.intersection(new_texts) or len(new_texts) != len(set(new_texts)):
        raise ValueError("memory unseen case reuses a source message")

    for domain in domains:
        group = [case for case in cases if case["domain"] == domain]
        if sum(not case["gold"]["writes"] for case in group) < 3:
            raise ValueError(f"{domain} needs at least three no-write cases")
        if sum(bool(case["gold"]["active_sources"])
               and not case["gold"]["recall_sources"] for case in group) < 3:
            raise ValueError(f"{domain} needs at least three no-recall cases with memory")
        if sum(len(case["gold"]["active_sources"]) >= 2
               and bool(case["gold"]["recall_sources"]) for case in group) < 2:
            raise ValueError(f"{domain} needs at least two retrieval distractor cases")
    for case in cases:
        by_id = {message["id"]: message for message in case["messages"]}
        for write in case["gold"]["writes"]:
            if write["fact"] not in by_id[write["source"]]["content"]:
                raise ValueError(f"non-extractive gold fact in {case['id']}")
    return cases


def main() -> None:
    cases = validate_unseen()
    print(f"Validated frozen unseen candidate: {len(cases)} cases (12 development, 12 personal)")


if __name__ == "__main__":
    main()
