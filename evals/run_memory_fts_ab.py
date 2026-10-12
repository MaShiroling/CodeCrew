"""Fixed-corpus legacy/BM25 A/B. Offline by default; --embed is billable.

Gold memories, not model-written memories, are seeded in disposable SQLite DBs.
Hybrid arms share one real embedding cache. Timed reads never call a provider.
"""

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.fts_text import FTS_TEXT_REVISION
from app.chat.vector_memory import EmbeddingEndpoint, MemoryVectorIndex, OpenAICompatibleEmbedder
from app.config import Settings
from evals.benchmark_memory_retrieval import UsageRecorder, latency_summary
from evals.check_memory_fts_baseline import FROZEN_PATH, _sha256, measure_legacy, verify_frozen
from evals.run_memory_retrieval_ab import _require_review, _scores, _seed_case
from evals.validate_memory_unseen import CASES_PATH, LOCK_PATH, validate_unseen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-fts-ab-v1"
LEXICAL_ARMS = ("legacy", "bm25")
ALL_ARMS = (*LEXICAL_ARMS, "legacy_hybrid", "bm25_hybrid")
CODE_PATHS = (
    "app/chat/memory.py", "app/chat/vector_memory.py", "app/chat/fts_memory.py",
    "app/chat/fts_text.py", "app/chat/store.py", "evals/run_memory_fts_ab.py",
    "evals/run_memory_retrieval_ab.py", "evals/benchmark_memory_retrieval.py",
    "evals/check_memory_fts_baseline.py", "evals/validate_memory_unseen.py",
)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class SharedEmbeddings:
    """Collect once per text hash, then freeze; a replay miss is a hard error.

    Fake embedders are only for unit tests. Experiment caches record provenance,
    and production CLI collection always constructs the real HTTP embedder.
    """

    def __init__(self, inner=None, *, max_calls=50, fingerprint=None):
        if type(max_calls) is not int or not 1 <= max_calls <= 50:
            raise ValueError("max_calls must be within 1–50")
        self.inner = inner
        self.model_fingerprint = fingerprint or inner.model_fingerprint
        self.max_calls = max_calls
        self.frozen = inner is None
        self.vectors = {}
        self.calls = self.texts = self.errors = 0
        self.request_ms = []
        self.dimensions = None

    @staticmethod
    def key(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _validate(self, vector):
        if (not isinstance(vector, (list, tuple)) or not 2 <= len(vector) <= 4096
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
            raise ValueError("invalid cached embedding vector")
        if self.dimensions is not None and len(vector) != self.dimensions:
            raise ValueError("embedding dimensions changed")
        self.dimensions = len(vector)
        return tuple(float(x) for x in vector)

    def embed(self, texts: Sequence[str]):
        try:
            if not texts or len(texts) > 32 or any(not text.strip() for text in texts):
                raise ValueError("invalid embedding input")
            missing = list(dict.fromkeys(text for text in texts if self.key(text) not in self.vectors))
            if missing:
                if self.frozen or self.inner is None:
                    raise RuntimeError("frozen embedding cache miss")
                if self.calls >= self.max_calls:
                    raise RuntimeError("embedding call cap reached")
                self.calls += 1
                self.texts += len(missing)
                started = time.perf_counter_ns()
                try:
                    raw = self.inner.embed(missing)
                finally:
                    self.request_ms.append((time.perf_counter_ns() - started) / 1_000_000)
                if len(raw) != len(missing):
                    raise ValueError("embedding count changed")
                checked = [self._validate(vector) for vector in raw]
                self.vectors.update({self.key(text): vector
                                     for text, vector in zip(missing, checked, strict=True)})
            return tuple(self.vectors[self.key(text)] for text in texts)
        except Exception:
            self.errors += 1
            raise

    def artifact(self, *, identity, complete):
        body = {"version": 1, "dataset_sha256": _sha256(CASES_PATH),
                "model_fingerprint": self.model_fingerprint, "identity": identity,
                "provenance": "cloud_api", "complete": complete,
                "vectors": self.vectors}
        return {**body, "sha256": _digest(body)}

    @classmethod
    def load(cls, path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        digest = payload.pop("sha256", None)
        if (digest != _digest(payload) or payload.get("version") != 1
                or payload.get("dataset_sha256") != _sha256(CASES_PATH)
                or payload.get("provenance") != "cloud_api" or payload.get("complete") is not True):
            raise ValueError("embedding cache provenance or checksum mismatch")
        fingerprint = payload.get("model_fingerprint")
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(x not in "0123456789abcdef" for x in fingerprint)):
            raise ValueError("invalid model fingerprint")
        identity = payload.get("identity")
        if (not isinstance(identity, dict) or set(identity) != {"model", "revision"}
                or any(not isinstance(x, str) or not x for x in identity.values())):
            raise ValueError("invalid cache model identity")
        result = cls(fingerprint=fingerprint)
        for key, vector in payload["vectors"].items():
            if (not isinstance(key, str) or len(key) != 64
                    or any(x not in "0123456789abcdef" for x in key)):
                raise ValueError("invalid embedding cache key")
            result.vectors[key] = result._validate(vector)
        return result, identity


def canonical_snapshot(store):
    """Include historical/inactive records and source messages, not derived indexes."""
    with store.database.connect() as connection:
        tables = ("standalone_chat_memories", "standalone_chat_messages")
        return _digest({table: sorted([dict(row) for row in connection.execute(
            f"SELECT * FROM {table}"
        )], key=lambda row: json.dumps(row, sort_keys=True)) for table in tables})


def recall(store, room, discussion, query, arm, index=None):
    """Detect swallowed production fallback; time only the retrieve invocation."""
    store.memory.lexical_backend = "bm25" if arm.startswith("bm25") else "legacy"
    store.memory.vector_index = index if arm.endswith("hybrid") else None
    observed = []
    vector_observed = []
    original = store.memory.fts_index.search
    vector_search = index.search if index else None

    def fts_search(*args, **kwargs):
        try:
            value = original(*args, **kwargs)
        except Exception:
            observed.append(False)
            raise
        observed.append(value is not None)
        return value

    def checked_vector(*args, **kwargs):
        try:
            value = vector_search(*args, **kwargs)
        except Exception:
            vector_observed.append(False)
            raise
        vector_observed.append(True)
        return value

    store.memory.fts_index.search = fts_search
    if index:
        index.search = checked_vector
    try:
        started = time.perf_counter_ns()
        result = store.memory.retrieve(room, query, exclude_correlation_id=discussion)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        store.memory.fts_index.search = original
        if index:
            index.search = vector_search
    if arm.startswith("bm25") and (observed != [True]
                                   or not store.memory.fts_index.status()["usable"]):
        raise RuntimeError("BM25 degraded; not a valid BM25 experiment")
    if False in vector_observed:
        raise RuntimeError("vector retrieval degraded; not a valid hybrid experiment")
    return result, elapsed


def compare_case(case, *, repeats=20, warmups=3, embeddings=None, case_index=0):
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ValueError("repeats must be within 1–100")
    if type(warmups) is not int or not 0 <= warmups <= 10:
        raise ValueError("warmups must be within 0–10")
    arms = ALL_ARMS if embeddings is not None else LEXICAL_ARMS
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-fts-ab-") as scratch:
        store, room, discussion, by_source = _seed_case(case, Path(scratch) / "chat.sqlite3")
        before = canonical_snapshot(store)
        with store.database.connect() as connection:
            eligible_count = connection.execute(
                "SELECT count(*) FROM standalone_chat_memories m JOIN standalone_chat_messages s "
                "ON s.message_id=m.source_message_id WHERE m.room_id=? AND m.status='active' "
                "AND s.correlation_id != ?", (str(room), str(discussion)),
            ).fetchone()[0]
        index = None
        was_frozen = embeddings.frozen if embeddings else True
        errors = embeddings.errors if embeddings else 0
        if embeddings:
            index = MemoryVectorIndex(store.database, embeddings, min_similarity=0.65)
            store.memory.vector_index = index
            for memory in store.memory.list_active(room):
                store.memory._index_if_enabled(memory)
            if embeddings.errors != errors:
                raise RuntimeError("embedding collection degraded")
            # Collect query embedding using the exact production normalization/guards.
            recall(store, room, discussion, case["query"]["content"], "legacy_hybrid", index)
            if embeddings.errors != errors:
                raise RuntimeError("embedding collection degraded")
            embeddings.frozen = True
        outputs = {}
        samples = {arm: [] for arm in arms}
        try:
            # Reverse order every round/case to reduce first-arm filesystem cache bias.
            for iteration in range(1 + warmups + repeats):
                order = arms if (iteration + case_index) % 2 == 0 else tuple(reversed(arms))
                for arm in order:
                    recalled, elapsed = recall(store, room, discussion, case["query"]["content"], arm, index)
                    sources = [by_source[memory.source_message_id] for memory in recalled]
                    if arm in outputs and sources != outputs[arm]:
                        raise RuntimeError("repeated retrieval changed ordered sources")
                    outputs[arm] = sources
                    if iteration > warmups:
                        samples[arm].append(elapsed)
            if canonical_snapshot(store) != before:
                raise RuntimeError("retrieval changed canonical records")
            if embeddings and embeddings.errors != errors:
                raise RuntimeError("cached hybrid retrieval degraded")
        finally:
            if embeddings:
                embeddings.frozen = was_frozen
        expected = set(case["gold"]["recall_sources"])
        return {"case_id": case["id"], "domain": case["domain"],
                "expected_sources": sorted(expected), "canonical_sha256": before,
                "same_canonical_snapshot": True, "eligible_candidates": min(eligible_count, 200),
                "arms": {arm: {"sources": outputs[arm], **_scores(expected, outputs[arm]),
                               "samples_ms": samples[arm], "latency": latency_summary(samples[arm])}
                         for arm in arms}}


def summarize(results):
    arms = tuple(results[0]["arms"])
    report = {"cases": len(results), "arms": {}, "pairs": {}}
    for arm in arms:
        counts = {key: sum(row["arms"][arm][key] for row in results) for key in ("tp", "fp", "fn")}
        tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
        positives = [row for row in results if row["expected_sources"]]
        ranks = [next((1 / rank for rank, source in enumerate(row["arms"][arm]["sources"], 1)
                       if source in row["expected_sources"]), 0) for row in positives]
        report["arms"][arm] = {**counts, "recall_at_3": tp / (tp + fn) if tp + fn else None,
                               "precision_at_3": tp / (tp + fp) if tp + fp else None,
                               "mrr_at_3": sum(ranks) / len(ranks) if ranks else None,
                               "false_positive_queries_with_empty_gold": sum(
                                   row["arms"][arm]["false_positive_on_empty_gold"] for row in results),
                               "latency": latency_summary([sample for row in results
                                                           for sample in row["arms"][arm]["samples_ms"]]),
                               "latency_by_candidate_presence": {
                                   label: latency_summary([sample for row in results
                                                           if bool(row["eligible_candidates"]) == present
                                                           for sample in row["arms"][arm]["samples_ms"]])
                                   for label, present in (("nonempty", True), ("empty", False))}}
    for old, new in (("legacy", "bm25"), ("legacy_hybrid", "bm25_hybrid")):
        if new not in arms:
            continue
        wins, losses, changed = [], [], []
        for row in results:
            a, b = row["arms"][old], row["arms"][new]
            if b["tp"] < a["tp"] or b["fp"] > a["fp"]:
                losses.append(row["case_id"])
            elif b["tp"] > a["tp"] or b["fp"] < a["fp"]:
                wins.append(row["case_id"])
            if a["sources"] != b["sources"]:
                changed.append(row["case_id"])
        report["pairs"][f"{old}_vs_{new}"] = {"wins": wins, "losses": losses,
                                               "ties": len(results) - len(wins) - len(losses),
                                               "ordered_sources_changed": changed}
    return report


def run_suite(*, output_root=RESULTS, repeats=20, warmups=3, embed=False, cache=None, max_calls=50):
    if embed and cache:
        raise ValueError("choose --embed or --cache, not both")
    # Check all parameters before loading private settings or making any request.
    if (type(repeats) is not int or not 1 <= repeats <= 100
            or type(warmups) is not int or not 0 <= warmups <= 10
            or type(max_calls) is not int or not 1 <= max_calls <= 50):
        raise ValueError("invalid experiment bounds")
    cases = validate_unseen()
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    _require_review(lock)
    verify_frozen(measure_legacy(cases), json.loads(FROZEN_PATH.read_text(encoding="utf-8")))
    embeddings, identity = None, None
    usage = UsageRecorder()
    if cache:
        embeddings, identity = SharedEmbeddings.load(cache)
    elif embed:
        settings = Settings()
        if settings.memory_embedding_mode != "on":
            raise ValueError("cloud embedding is not configured")
        endpoint = EmbeddingEndpoint(
            base_url=settings.memory_embedding_base_url, model=settings.memory_embedding_model,
            revision=settings.memory_embedding_revision,
            api_key=settings.memory_embedding_api_key.get_secret_value(),
            timeout_seconds=settings.memory_embedding_timeout_seconds,
        )
        identity = {"model": endpoint.model, "revision": endpoint.revision}
        embeddings = SharedEmbeddings(OpenAICompatibleEmbedder(endpoint, on_usage_tokens=usage.record),
                                      max_calls=max_calls)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    result_dir = output_root / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"version": 1, "complete": False,
                "mode": "collect_cloud" if embed else "cached_replay" if cache else "offline_lexical",
                "dataset_sha256": _sha256(CASES_PATH), "label_lock_sha256": _sha256(LOCK_PATH),
                "legacy_freeze_sha256": _sha256(FROZEN_PATH),
                "code_sha256": {path: _sha256(ROOT / path) for path in CODE_PATHS},
                "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version,
                "platform": platform.platform(), "fts_revision": FTS_TEXT_REVISION,
                "parameters": {"limit": 3, "candidate_limit": 200, "rrf_constant": 60,
                               "min_similarity": 0.65, "repeats": repeats, "warmups": warmups},
                "embedding": identity, "agent_calls": 0, "billing_cost": None,
                "cache_input_sha256": _sha256(cache) if cache else None,
                "timing_scope": "warm local retrieve; hybrid uses frozen embeddings; excludes cloud, "
                                "seeding/startup/indexing/Agent; alternated arm order",
                "label_review": {key: lock[key] for key in ("reviewer", "reviewed_at")}}

    def checkpoint():
        manifest.update({"embedding_calls": embeddings.calls if embeddings else 0,
                         "embedding_texts": embeddings.texts if embeddings else 0,
                         "embedding_errors": embeddings.errors if embeddings else 0,
                         "provider_latency": latency_summary(embeddings.request_ms if embeddings else []),
                         "usage_reported_calls": usage.reported_calls,
                         "reported_tokens": usage.total_tokens,
                         "embedding_fingerprint": embeddings.model_fingerprint if embeddings else None})
        if embeddings:
            payload = embeddings.artifact(identity=identity, complete=manifest["complete"])
            (result_dir / "embedding-cache.json").write_text(json.dumps(payload), encoding="utf-8")
            manifest["embedding_cache_sha256"] = _sha256(result_dir / "embedding-cache.json")
        (result_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")

    results = []
    checkpoint()
    try:
        for number, case in enumerate(cases):
            print(f"[{number + 1}/{len(cases)}] {case['id']}", flush=True)
            row = compare_case(case, repeats=repeats, warmups=warmups,
                               embeddings=embeddings, case_index=number)
            results.append(row)
            (result_dir / f"{case['id']}.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            checkpoint()
        frozen_rows = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))["legacy"]["results"]
        if {row["case_id"]: row["arms"]["legacy"]["sources"] for row in results} != {
                row["case_id"]: row["sources"] for row in frozen_rows}:
            raise RuntimeError("paired legacy sources differ from frozen baseline")
        (result_dir / "summary.json").write_text(
            json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["complete"] = True
        checkpoint()
    except Exception as exc:
        manifest["complete"] = False
        manifest["error_type"] = type(exc).__name__
        checkpoint()
        print(f"Incomplete experiment: {result_dir}", flush=True)
        raise
    return result_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--embed", action="store_true", help="explicitly collect cloud embeddings")
    mode.add_argument("--cache", type=Path, help="replay a complete collected cache, no network/settings")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--max-calls", type=int, default=50)
    args = parser.parse_args()
    try:
        path = run_suite(repeats=args.repeats, warmups=args.warmups, embed=args.embed,
                         cache=args.cache, max_calls=args.max_calls)
    except Exception as exc:  # noqa: BLE001 - report categories without exposing provider secrets
        parser.exit(1, f"Experiment failed: {type(exc).__name__}; not scored as retrieval misses\n")
    print(f"Results: {path}")


if __name__ == "__main__":
    main()
