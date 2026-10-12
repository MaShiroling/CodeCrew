"""Measure memory indexing and retrieval latency with bounded cloud usage.

Default mode performs only validation. ``--live`` explicitly calls the cloud
embedding API. It never calls an Agent or reads real chat data.
"""

import argparse
import hashlib
import json
import math
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

from app.chat.vector_memory import (
    Embedder,
    EmbeddingEndpoint,
    MemoryVectorIndex,
    OpenAICompatibleEmbedder,
)
from app.config import Settings
from evals.run_memory_retrieval_ab import _require_review, _seed_case
from evals.validate_memory_unseen import LOCK_PATH, validate_unseen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results" / "memory-retrieval-benchmark-v1"


class UsageRecorder:
    """Only aggregate API-reported token counts; never keep request text."""

    def __init__(self) -> None:
        self.reported_calls = 0
        self.total_tokens = 0

    def record(self, tokens: int) -> None:
        self.reported_calls += 1
        self.total_tokens += tokens


class MeteredEmbedder:
    def __init__(self, inner: Embedder, *, max_calls: int) -> None:
        self.inner = inner
        self.model_fingerprint = inner.model_fingerprint
        self.max_calls = max_calls
        self.calls = 0
        self.texts = 0
        self.errors = 0
        self.request_ms: list[float] = []

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if self.calls >= self.max_calls:
            self.errors += 1
            raise RuntimeError("embedding call cap reached")
        self.calls += 1
        self.texts += len(texts)
        started = time.perf_counter_ns()
        try:
            return self.inner.embed(texts)
        except Exception:
            self.errors += 1
            raise
        finally:
            self.request_ms.append((time.perf_counter_ns() - started) / 1_000_000)


def _timed(function: Callable[[], object]) -> tuple[object, float]:
    started = time.perf_counter_ns()
    result = function()
    return result, (time.perf_counter_ns() - started) / 1_000_000


def latency_summary(samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {"count": 0, "p50_ms": None, "p95_ms": None,
                "min_ms": None, "max_ms": None}
    ordered = sorted(samples_ms)
    return {
        "count": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def estimate_original_price(tokens: int, *, price_per_million_cny: Decimal,
                            usage_complete: bool) -> str | None:
    if not usage_complete:
        return None
    return str((Decimal(tokens) * price_per_million_cny / Decimal(1_000_000))
               .quantize(Decimal("0.00000001")))


def _recall(store, room_id, discussion_id, query: str):
    return store.memory.retrieve(
        room_id, query, exclude_correlation_id=discussion_id,
    )


def _all_active_memories(store):
    with store.database.connect() as connection:
        room_ids = [UUID(row["room_id"]) for row in connection.execute(
            "SELECT DISTINCT room_id FROM standalone_chat_memories WHERE status = 'active'"
        ).fetchall()]
    return tuple(memory for room_id in room_ids
                 for memory in store.memory.list_active(room_id))


def benchmark_case(case: dict, embedder: MeteredEmbedder, *, min_similarity: float,
                   repeats: int) -> dict:
    """Measure local lexical, production-style write indexing, warm and cold reads."""
    if not 1 <= repeats <= 5:
        raise ValueError("repeats must be within 1–5")
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-benchmark-") as scratch:
        root = Path(scratch)
        store, room_id, discussion_id, source_by_id = _seed_case(
            case, root / "indexed.sqlite3",
        )
        query = case["query"]["content"]
        samples: dict[str, list[float]] = {
            "lexical": [], "write_index": [], "warm_cloud": [],
            "warm_local": [], "cold_cloud": [], "cold_local": [],
        }
        lexical_sources = None
        for _ in range(repeats):
            memories, ms = _timed(lambda: _recall(store, room_id, discussion_id, query))
            sources = [source_by_id[memory.source_message_id] for memory in memories]
            if lexical_sources is not None and sources != lexical_sources:
                raise RuntimeError("lexical result changed during benchmark")
            lexical_sources = sources
            samples["lexical"].append(ms)

        store.memory.vector_index = MemoryVectorIndex(
            store.database, embedder, min_similarity=min_similarity,
        )
        active_in_query_room = store.memory.list_active(room_id)
        all_active = _all_active_memories(store)
        for memory in all_active:
            calls_before = embedder.calls
            _, ms = _timed(lambda memory=memory: store.memory._index_if_enabled(memory))
            if embedder.errors:
                raise RuntimeError("embedding failed during write-index benchmark")
            if embedder.calls > calls_before:
                samples["write_index"].append(ms)

        warm_sources = None
        for _ in range(repeats):
            calls_before = embedder.calls
            memories, ms = _timed(lambda: _recall(store, room_id, discussion_id, query))
            if embedder.errors:
                raise RuntimeError("embedding failed during warm-read benchmark")
            sources = [source_by_id[memory.source_message_id] for memory in memories]
            if warm_sources is not None and sources != warm_sources:
                raise RuntimeError("warm result changed during benchmark")
            warm_sources = sources
            samples["warm_cloud" if embedder.calls > calls_before else "warm_local"].append(ms)

        # A fresh database models the first read when the derived vector index
        # is absent. Production usually indexes on write; this is its backfill path.
        cold_store, cold_room, cold_discussion, cold_source_by_id = _seed_case(
            case, root / "unindexed.sqlite3",
        )
        cold_store.memory.vector_index = MemoryVectorIndex(
            cold_store.database, embedder, min_similarity=min_similarity,
        )
        calls_before = embedder.calls
        cold_memories, ms = _timed(
            lambda: _recall(cold_store, cold_room, cold_discussion, query)
        )
        if embedder.errors:
            raise RuntimeError("embedding failed during cold-read benchmark")
        cold_sources = [cold_source_by_id[memory.source_message_id]
                        for memory in cold_memories]
        samples["cold_cloud" if embedder.calls > calls_before else "cold_local"].append(ms)
        if cold_sources != warm_sources:
            raise RuntimeError("cold and warm results disagree")
        return {
            "case_id": case["id"], "domain": case["domain"],
            "active_memories_in_query_room": len(active_in_query_room),
            "active_memories_total": len(all_active),
            "lexical_sources": lexical_sources,
            "hybrid_sources": warm_sources,
            "expected_sources": case["gold"]["recall_sources"],
            "samples_ms": samples,
        }


def summarize(results: list[dict], embedder: MeteredEmbedder,
              usage: UsageRecorder, *, price_per_million_cny: Decimal | None) -> dict:
    stage_names = ("lexical", "write_index", "warm_cloud", "warm_local",
                   "cold_cloud", "cold_local")
    samples = {name: [ms for case in results for ms in case["samples_ms"][name]]
               for name in stage_names}
    usage_complete = usage.reported_calls == embedder.calls
    return {
        "cases": len(results),
        "latency": {name: latency_summary(values) for name, values in samples.items()},
        "provider_request_latency": latency_summary(embedder.request_ms),
        "embedding_calls": embedder.calls,
        "embedding_texts": embedder.texts,
        "api_usage_total_tokens": usage.total_tokens,
        "api_usage_reported_calls": usage.reported_calls,
        "api_usage_complete": usage_complete,
        "original_price_estimate_cny": (
            estimate_original_price(
                usage.total_tokens, price_per_million_cny=price_per_million_cny,
                usage_complete=usage_complete,
            ) if price_per_million_cny is not None else None
        ),
        "actual_billed_cny": None,
    }


def run_suite(*, repeats: int, max_calls: int,
              price_per_million_cny: Decimal | None,
              output_root: Path = RESULTS) -> Path:
    if not 1 <= repeats <= 5 or not 1 <= max_calls <= 200:
        raise ValueError("benchmark repeat or call cap is out of bounds")
    if price_per_million_cny is not None and price_per_million_cny < 0:
        raise ValueError("price must be nonnegative")
    cases = validate_unseen()
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    _require_review(lock)
    settings = Settings()
    if settings.memory_embedding_mode != "on":
        raise ValueError("cloud embedding is not configured")
    endpoint = EmbeddingEndpoint(
        base_url=settings.memory_embedding_base_url,
        model=settings.memory_embedding_model,
        revision=settings.memory_embedding_revision,
        api_key=settings.memory_embedding_api_key.get_secret_value(),
        timeout_seconds=settings.memory_embedding_timeout_seconds,
    )
    usage = UsageRecorder()
    embedder = MeteredEmbedder(
        OpenAICompatibleEmbedder(endpoint, on_usage_tokens=usage.record),
        max_calls=max_calls,
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    result_dir = output_root / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = benchmark_case(
            case, embedder, min_similarity=settings.memory_embedding_min_similarity,
            repeats=repeats,
        )
        results.append(result)
        (result_dir / f"{case['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    manifest = {
        "dataset_sha256": lock["sha256"],
        "code_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("app/chat/memory.py", "app/chat/vector_memory.py",
                         "evals/run_memory_retrieval_ab.py",
                         "evals/benchmark_memory_retrieval.py")
        },
        "embedding": {"model": endpoint.model, "revision": endpoint.revision,
                      "fingerprint": endpoint.model_fingerprint,
                      "min_similarity": settings.memory_embedding_min_similarity},
        "repeats": repeats, "max_embedding_calls": max_calls,
        "agent_calls": 0,
        "price_per_million_cny": (str(price_per_million_cny)
                                  if price_per_million_cny is not None else None),
        "price_basis": "operator_supplied_public_list_price_not_invoice",
    }
    (result_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (result_dir / "summary.json").write_text(
        json.dumps(summarize(results, embedder, usage,
                             price_per_million_cny=price_per_million_cny),
                   ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="make billable cloud embedding calls")
    parser.add_argument("--repeats", type=int, default=3,
                        help="lexical and warm-query repetitions per case (1–5)")
    parser.add_argument("--max-calls", type=int, default=160,
                        help="hard cap on embedding requests (1–200)")
    parser.add_argument("--price-per-million-cny",
                        help="optional public list price, only for non-billing estimate")
    args = parser.parse_args()
    try:
        price = (Decimal(args.price_per_million_cny)
                 if args.price_per_million_cny is not None else None)
    except InvalidOperation as exc:
        parser.error(f"invalid price: {exc}")
    if args.live:
        result_dir = run_suite(
            repeats=args.repeats, max_calls=args.max_calls,
            price_per_million_cny=price,
        )
        print(f"Results: {result_dir}")
    else:
        cases = validate_unseen()
        print(f"Validated {len(cases)} frozen cases; no cloud requests made")


if __name__ == "__main__":
    main()
