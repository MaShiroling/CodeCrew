"""Validate the fixed, human-labeled memory baseline without invoking agents."""

import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "memory-baseline-v1.jsonl"
SCHEMA_PATH = ROOT / "memory-baseline-v1.schema.json"
SPLITS_PATH = ROOT / "memory-baseline-v1.splits.json"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    """Read a versioned JSONL fixture, preserving its line numbers in errors."""
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"blank JSONL line {line_number}")
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line {line_number}: {exc.msg}") from exc
    return cases


def validate_cases(cases: list[dict], schema_path: Path = SCHEMA_PATH) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    seen_cases: set[str] = set()
    for case in cases:
        validator.validate(case)
        case_id = case["id"]
        if case_id in seen_cases:
            raise ValueError(f"duplicate case id: {case_id}")
        seen_cases.add(case_id)
        expected_prefix = "dev_" if case["domain"] == "development" else "personal_"
        if not case_id.startswith(expected_prefix):
            raise ValueError(f"case id/domain mismatch: {case_id}")

        messages = case["messages"]
        by_id = {message["id"]: message for message in messages}
        if len(by_id) != len(messages):
            raise ValueError(f"duplicate message id in {case_id}")
        writes = case["gold"]["writes"]
        written: dict[str, dict] = {}
        replaced: set[str] = set()
        for write in writes:
            source_id = write["source"]
            source = by_id.get(source_id)
            if source is None or source_id in written:
                raise ValueError(f"invalid write source {source_id} in {case_id}")
            if source["role"] != "human" or source.get("external", False):
                raise ValueError(f"non-local-human write source {source_id} in {case_id}")
            if any(
                message.get("external", False)
                for message in messages
                if message["room"] == source["room"]
                and message["discussion"] == source["discussion"]
            ):
                raise ValueError(f"external discussion write source {source_id} in {case_id}")
            old_id = write.get("replaces")
            if old_id:
                if old_id not in written or old_id in replaced:
                    raise ValueError(f"invalid replacement {old_id} in {case_id}")
                if by_id[old_id]["room"] != source["room"]:
                    raise ValueError(f"cross-room replacement {old_id} in {case_id}")
                replaced.add(old_id)
            written[source_id] = write

        active = set(written) - replaced
        if set(case["gold"]["active_sources"]) != active:
            raise ValueError(f"active memory labels disagree with writes in {case_id}")
        query = case["query"]
        eligible = {
            source_id for source_id in active
            if by_id[source_id]["room"] == query["room"]
            and by_id[source_id]["discussion"] != query["discussion"]
            and not query.get("external", False)
        }
        if not set(case["gold"]["recall_sources"]) <= eligible:
            raise ValueError(f"recall label violates scope or discussion boundary in {case_id}")


def load_splits(path: Path = SPLITS_PATH) -> dict[str, list[str]]:
    splits = json.loads(path.read_text(encoding="utf-8"))
    if splits.get("version") != 1:
        raise ValueError("unsupported memory baseline split version")
    return {name: splits[name] for name in ("tuning", "holdout")}


def validate_splits(cases: list[dict], splits: dict[str, list[str]]) -> None:
    if set(splits) != {"tuning", "holdout"}:
        raise ValueError("memory baseline requires tuning and holdout splits")
    all_ids = [case_id for group in splits.values() for case_id in group]
    if len(all_ids) != len(set(all_ids)) or set(all_ids) != {case["id"] for case in cases}:
        raise ValueError("memory baseline splits must partition every case exactly once")
    by_id = {case["id"]: case for case in cases}
    for name, expected_size in (("tuning", 12), ("holdout", 8)):
        if len(splits[name]) != expected_size:
            raise ValueError(f"{name} split must contain {expected_size} cases")
        domains = Counter(by_id[case_id]["domain"] for case_id in splits[name])
        if domains["development"] != expected_size // 2 or domains["personal"] != expected_size // 2:
            raise ValueError(f"{name} split must balance development and personal cases")


def main() -> None:
    cases = load_cases()
    validate_cases(cases)
    validate_splits(cases, load_splits())
    domains = Counter(case["domain"] for case in cases)
    print(f"Validated {len(cases)} memory cases: {dict(sorted(domains.items()))}")


if __name__ == "__main__":
    main()
