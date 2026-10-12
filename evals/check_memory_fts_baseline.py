"""Offline FTS5 readiness probe and frozen legacy retrieval comparison.

Only synthetic, disposable SQLite databases are opened. No Settings instance,
Agent, cloud embedder, application database, or extension loader is used.
This checks readiness; it does not enable FTS5 in production or choose a tokenizer.
"""

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from evals.run_memory_retrieval_ab import _scores, _seed_case
from evals.validate_memory_unseen import CASES_PATH, LOCK_PATH, validate_unseen

ROOT = Path(__file__).resolve().parents[1]
FROZEN_PATH = ROOT / "evals" / "memory-fts-legacy-v1.json"
CODE_PATHS = (
    "app/chat/memory.py", "app/chat/vector_memory.py", "app/chat/dispatch.py",
    "evals/run_memory_retrieval_ab.py", "evals/check_memory_fts_baseline.py",
)
CONTRACT = {
    "interface": "retrieve(room_id, query, *, limit=3, exclude_correlation_id=None)",
    "result": "ordered tuple of canonical ChatMemory records",
    "lexical": "casefold ASCII words and Chinese bigrams; set overlap count descending",
    "lexical_tiebreak": "created_at descending; stable input order on ties",
    "candidate_scope": "same room, active only, exclude source's current discussion",
    "candidate_limit": 200,
    "recall_limit": 3,
    "allowed_limits": [1, 2, 3],
    "external_discussion_boundary": "dispatcher skips recall; not a retrieve argument",
    "seed": "human-approved gold original text via manual API; no Agent extraction",
    "seed_kind": "fact; gold kinds are not evaluated by this retrieval baseline",
    "hybrid_rrf_constant": 60,
    "hybrid_threshold_for_later_comparison": 0.65,
    "hybrid_measured_here": False,
    "chinese_fts_tokenizer": "not selected in this step",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe_fts5() -> dict:
    """Compile flag is advisory; actual create/MATCH/bm25 is authoritative."""
    report = {
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "compile_option_enable_fts5": False,
        "fts5_supported": False,
        "bm25_supported": False,
        "error_stage": None,
        "error_type": None,
    }
    stage = "connect"
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            stage = "compile_options"
            report["compile_option_enable_fts5"] = any(
                row[0] == "ENABLE_FTS5" for row in connection.execute("PRAGMA compile_options")
            )
            stage = "create_and_match"
            connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(content)")
            connection.executemany("INSERT INTO fts_probe(content) VALUES (?)", [
                ("gateway timeout",), ("audit retention",),
            ])
            rows = connection.execute(
                "SELECT rowid FROM fts_probe WHERE fts_probe MATCH ?", ("gateway",),
            ).fetchall()
            if rows != [(1,)]:
                raise ValueError("unexpected FTS5 MATCH result")
            report["fts5_supported"] = True
            stage = "bm25"
            ranked = connection.execute(
                "SELECT rowid, bm25(fts_probe) FROM fts_probe "
                "WHERE fts_probe MATCH ? ORDER BY bm25(fts_probe)", ("gateway",),
            ).fetchall()
            if (len(ranked) != 1 or ranked[0][0] != 1
                    or not math.isfinite(ranked[0][1])):
                raise ValueError("unexpected BM25 result")
            report["bm25_supported"] = True
    except (sqlite3.Error, ValueError) as exc:
        # Report a safe category, not arbitrary error text, settings or secrets.
        report["error_stage"] = stage
        report["error_type"] = type(exc).__name__
    return report


def measure_legacy(cases: list[dict]) -> dict:
    """Run only the existing lexical path, on fresh labeled snapshots."""
    results = []
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-fts-baseline-") as scratch:
        for case in cases:
            store, room_id, discussion_id, source_by_id = _seed_case(
                case, Path(scratch) / f"{len(results)}.sqlite3",
            )
            if store.memory.vector_index is not None:
                raise ValueError("offline baseline must not construct a vector index")
            before = store.memory.list_active(room_id)
            recalled = store.memory.retrieve(
                room_id, case["query"]["content"], limit=3,
                exclude_correlation_id=discussion_id,
            )
            if before != store.memory.list_active(room_id):
                raise ValueError("retrieval changed canonical active memory")
            actual = [source_by_id[memory.source_message_id] for memory in recalled]
            expected = set(case["gold"]["recall_sources"])
            results.append({
                "case_id": case["id"], "sources": actual,
                **_scores(expected, actual),
            })
    counts = {key: sum(row[key] for row in results) for key in ("tp", "fp", "fn")}
    summary = {
        "cases": len(results), **counts,
        "recall_at_3": (counts["tp"] / (counts["tp"] + counts["fn"])
                        if counts["tp"] + counts["fn"] else None),
        "precision_at_3": (counts["tp"] / (counts["tp"] + counts["fp"])
                           if counts["tp"] + counts["fp"] else None),
        "false_positive_queries_with_empty_gold": sum(
            row["false_positive_on_empty_gold"] for row in results
        ),
    }
    return {"summary": summary, "results": results}


def verify_frozen(measured: dict, frozen: dict) -> None:
    """Never rewrite expected results when implementation or labels drift."""
    if frozen.get("version") != 1 or frozen.get("dataset") != CASES_PATH.name:
        raise ValueError("unsupported legacy baseline freeze")
    if frozen.get("dataset_sha256") != _sha256(CASES_PATH):
        raise ValueError("legacy baseline dataset changed")
    if frozen.get("label_lock_sha256") != _sha256(LOCK_PATH):
        raise ValueError("legacy baseline label review changed")
    if frozen.get("contract") != CONTRACT:
        raise ValueError("legacy retrieval contract changed")
    if measured != frozen.get("legacy"):
        raise ValueError("legacy ordered sources or metrics differ from frozen baseline")


def inspect_baseline() -> dict:
    cases = validate_unseen()
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    measured = measure_legacy(cases)
    verify_frozen(measured, frozen)
    hashes = {name: _sha256(ROOT / name) for name in CODE_PATHS}
    return {
        "capability": probe_fts5(),
        "baseline_matches_freeze": True,
        "dataset_sha256": _sha256(CASES_PATH),
        "label_lock_sha256": _sha256(LOCK_PATH),
        "contract": CONTRACT,
        "code_sha256": hashes,
        # Code hashes are provenance, not a ban on later code changes. Ordered
        # legacy results and dataset/review hashes remain the checked contract.
        "code_changed_since_freeze": [
            name for name, digest in hashes.items()
            if digest != frozen["code_sha256"].get(name)
        ],
        "agent_calls": 0, "embedding_calls": 0,
        "legacy": measured,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print full safe report to stdout")
    args = parser.parse_args()
    try:
        report = inspect_baseline()
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Baseline check failed: {exc}\n")
    capability = report["capability"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Python {capability['python_version']} / SQLite {capability['sqlite_version']}")
        print(f"FTS5: {capability['fts5_supported']} / BM25: {capability['bm25_supported']}")
        summary = report["legacy"]["summary"]
        print(f"Frozen legacy: {summary['cases']} cases, "
              f"TP/FP/FN={summary['tp']}/{summary['fp']}/{summary['fn']}, "
              f"Recall@3={summary['recall_at_3']:.2%}; ordered sources match")
        print("Agent/embedding calls: 0/0; no production database or config opened")
        if report["code_changed_since_freeze"]:
            print("Code provenance changed: " + ", ".join(report["code_changed_since_freeze"]))
    if not capability["fts5_supported"] or not capability["bm25_supported"]:
        parser.exit(1, "FTS5/BM25 is unavailable in this Python SQLite runtime\n")


if __name__ == "__main__":
    main()
