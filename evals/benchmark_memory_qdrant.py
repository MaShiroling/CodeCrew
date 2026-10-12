"""Paired cached-cloud quality and synthetic scale benchmark; plan only by default.

--live --cache creates one owned Docker instance, never uses settings/cloud/Agent
or real chat data, then removes only that container. Evidence bind data remains.
Exact scoped search, not ANN. Timings exclude seeding/delivery/audit/cloud.
"""

import argparse
import json
import platform
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.fts_text import FTS_TEXT_REVISION
from app.chat.memory import _MENTION, _SENSITIVE
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.service import StandaloneChatService
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_contracts import VectorModel, VectorSearch
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_memory import MemoryVectorIndex
from app.chat.vector_outbox import MemoryVectorOutbox
from app.storage import SQLiteDatabase
from evals.benchmark_memory_retrieval import latency_summary
from evals.check_memory_fts_baseline import FROZEN_PATH, _sha256, measure_legacy, verify_frozen
from evals.check_memory_qdrant_faults import IMAGE, OwnedDocker, require
from evals.run_memory_fts_ab import SharedEmbeddings, canonical_snapshot
from evals.run_memory_retrieval_ab import _require_review, _scores, _seed_case
from evals.validate_memory_unseen import CASES_PATH, LOCK_PATH, validate_unseen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-qdrant-benchmark-v1"
ARMS = ("bm25", "sqlite_hybrid", "qdrant_hybrid")
SCALES = ((10, 10), (50, 50), (200, 200), (200, 1000))
THRESHOLD = .65


def bounds(repeats, warmups, scales):
    if (type(repeats) is not int or not 1 <= repeats <= 100 or
            type(warmups) is not int or not 0 <= warmups <= 10 or
            not isinstance(scales, tuple) or not 1 <= len(scales) <= 8):
        raise ValueError("invalid benchmark bounds")
    for pair in scales:
        if (not isinstance(pair, tuple) or len(pair) != 2 or
                any(type(n) is not int for n in pair) or not 1 <= pair[0] <= 200 or
                not pair[0] <= pair[1] <= 2000):
            raise ValueError("invalid bounded candidate/global scale")
    if len(set(scales)) != len(scales):
        raise ValueError("duplicate scale")


class ObservedIndex:
    """Detect exceptions swallowed by production fallback, including scoped API."""
    def __init__(self, inner):
        self.inner, self.calls, self.errors = inner, 0, 0

    def index(self, memories):
        return self.inner.index(memories)

    def search_scoped(self, query, memories, **scope):
        self.calls += 1
        try:
            method = getattr(self.inner, "search_scoped", None)
            return method(query, memories, **scope) if method else self.inner.search(query, memories)
        except Exception:
            self.errors += 1
            raise

    def search(self, query, memories):
        return self.search_scoped(query, memories)


class RemoteMeter:
    def __init__(self, backend):
        self.calls = 0
        original = backend._request
        def request(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)
        backend._request = request


def measured_recall(store, room, discussion, query, index):
    store.memory.lexical_backend = "bm25"
    store.memory.vector_index = index
    observed = []
    original = store.memory.fts_index.search
    def checked(*args, **kwargs):
        try:
            value = original(*args, **kwargs)
            observed.append(value is not None)
            return value
        except Exception:
            observed.append(False)
            raise
    store.memory.fts_index.search = checked
    errors = index.errors if index else 0
    try:
        started = time.perf_counter_ns()
        result = store.memory.retrieve(room, query, exclude_correlation_id=discussion)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        store.memory.fts_index.search = original
    require(observed == [True] and store.memory.fts_index.status()["usable"], "BM25_degraded")
    require(index is None or index.errors == errors, "vector_retrieval_degraded")
    return result, elapsed


def paired_reads(store, room, discussion, query, by_source, expected, indexes, *, repeats, warmups, offset=0):
    outputs, samples = {}, {arm: [] for arm in ARMS}
    for iteration in range(warmups + repeats):
        order = ARMS if (iteration + offset) % 2 == 0 else tuple(reversed(ARMS))
        for arm in order:
            memories, ms = measured_recall(store, room, discussion, query, indexes.get(arm))
            sources = [by_source[m.source_message_id] for m in memories]
            require(arm not in outputs or sources == outputs[arm], "unstable_ordered_sources")
            outputs[arm] = sources
            if iteration >= warmups:
                samples[arm].append(ms)
    return {arm: {"sources": outputs[arm], **_scores(set(expected), outputs[arm]),
                  "samples_ms": samples[arm], "latency": latency_summary(samples[arm])} for arm in ARMS}


def prepared_indexes(store, embeddings, backend, endpoint):
    local = MemoryVectorIndex(store.database, embeddings, min_similarity=THRESHOLD, backfill_limit=0)
    remote = QdrantMemoryVectorIndex(store.database, embeddings, endpoint, min_similarity=THRESHOLD,
                                   backend_factory=lambda model: backend)
    return {"sqlite_hybrid": ObservedIndex(local), "qdrant_hybrid": ObservedIndex(remote)}


def drain(worker):
    for _ in range(32):
        if worker.run_once(limit=200) == 0:
            require(not any(worker.status().get(s) for s in ("pending", "processing", "waiting", "dead")),
                    "benchmark_delivery_incomplete")
            return
    raise RuntimeError("benchmark_delivery_budget")


def finish_audit(worker):
    maintenance = MemoryVectorMaintenance(worker)
    run = maintenance.start(mode="audit")
    for _ in range(256):
        status = maintenance.step(run, limit=16)
        if status["scan_complete"]:
            require(status["consistent_at_observation"], "benchmark_audit_inconsistent")
            return status
    raise RuntimeError("benchmark_audit_budget")


def storage_parity(store, room, discussion, query, embeddings, worker):
    """Untimed full vector ranking and score checks, independent of final top 3."""
    with store.database.connect() as conn:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE room_id=?", (str(room),)).fetchall()
        mutations = tuple(worker.desired_in_snapshot(conn, row)[0] for row in rows)
    mutations = tuple(m for m in mutations if m.record and m.record.anchor.source_correlation_id != discussion)
    if not mutations or _SENSITIVE.search(query):
        return {"candidates": 0, "ordered_ids_equal": True, "max_score_delta": 0., "threshold_boundary_ids": []}
    vector = embeddings.embed([_MENTION.sub("", query).strip()])[0]
    request = VectorSearch(room, worker.backend.model, vector, tuple(m.record.anchor for m in mutations),
                           exclude_correlation_id=discussion, limit=len(mutations), min_similarity=THRESHOLD)
    local = SQLiteMemoryVectorBackend(store.database).search(request)
    remote = worker.backend.search_current(request, mutations)
    a, b = {h.anchor.memory_id: h.similarity for h in local}, {h.anchor.memory_id: h.similarity for h in remote}
    common = set(a) & set(b)
    return {"candidates": len(mutations), "ordered_ids_equal": [h.anchor.memory_id for h in local] ==
            [h.anchor.memory_id for h in remote], "max_score_delta": max((abs(a[k] - b[k]) for k in common), default=0.),
            "sqlite_only_ids": sorted(str(k) for k in set(a) - set(b)),
            "qdrant_only_ids": sorted(str(k) for k in set(b) - set(a)),
            "threshold_boundary_ids": sorted(str(k) for k, score in (a | b).items() if abs(score - THRESHOLD) < 1e-5)}


def quality_suite(cases, path, embeddings, backend, endpoint, *, repeats, warmups, progress=print, read_runner=None):
    require(embeddings.frozen and embeddings.inner is None, "quality_requires_frozen_cache")
    seeded = [_seed_case(case, path) for case in cases]
    store = seeded[-1][0]
    before = canonical_snapshot(store)
    worker = MemoryVectorOutbox(store.database, backend)
    worker.register_target()  # No history scan: only subsequent relevant cache writes fan out.
    indexes = prepared_indexes(store, embeddings, backend, endpoint)
    errors = embeddings.errors
    indexed = set()
    for _, room, _, _ in seeded:
        for memory in store.memory.list_active(room):
            if memory.memory_id not in indexed and not _SENSITIVE.search(memory.content):
                indexes["sqlite_hybrid"].index((memory,))
                indexed.add(memory.memory_id)
    require(embeddings.errors == errors, "quality_cache_miss")
    drain(worker)
    meter = RemoteMeter(backend)
    rows = []
    for i, (case, (_, room, discussion, by_source)) in enumerate(zip(cases, seeded, strict=True)):
        progress(f"[quality {i+1}/{len(cases)}] {case['id']}", flush=True)
        query = case["query"]["content"]
        parity = storage_parity(store, room, discussion, query, embeddings, worker)
        prior = meter.calls
        arms = (read_runner or paired_reads)(store, room, discussion, query, by_source, case["gold"]["recall_sources"], indexes,
                            repeats=repeats, warmups=warmups, offset=i)
        with store.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM standalone_chat_memories m JOIN standalone_chat_messages s "
                "ON m.source_message_id=s.message_id WHERE m.room_id=? AND m.status='active' AND s.correlation_id!=?",
                (str(room), str(discussion))).fetchone()[0]
        rows.append({"case_id": case["id"], "domain": case["domain"], "expected_sources": case["gold"]["recall_sources"],
                     "eligible_candidates": count, "arms": arms, "storage_parity": parity,
                     "read_http_requests_including_warmups": meter.calls-prior})
    require(canonical_snapshot(store) == before, "quality_canonical_changed")
    require(embeddings.errors == errors and embeddings.calls == 0, "quality_provider_or_cache_error")
    summary = summarize(rows)
    summary["paired_storage"] = paired_summary(rows)
    summary["outbox_status"] = worker.status()
    summary["cache_scope"] = "gold active query-room memories only; unqueried other-room history not backfilled/audited"
    return rows, summary


def paired_summary(rows):
    wins, losses, changed = [], [], []
    for row in rows:
        a, b = (row["arms"][arm] for arm in ("sqlite_hybrid", "qdrant_hybrid"))
        if b["tp"] < a["tp"] or b["fp"] > a["fp"]:
            losses.append(row["case_id"])
        elif b["tp"] > a["tp"] or b["fp"] < a["fp"]:
            wins.append(row["case_id"])
        if a["sources"] != b["sources"]:
            changed.append(row["case_id"])
    return {"wins": wins, "losses": losses, "ordered_sources_changed": changed,
            "full_vector_order_changed": [r["case_id"] for r in rows if not r["storage_parity"]["ordered_ids_equal"]],
            "max_score_delta": max((r["storage_parity"]["max_score_delta"] for r in rows), default=0.)}


def summarize(rows):
    result = {"cases": len(rows), "arms": {}}
    positives = [r for r in rows if r["expected_sources"]]
    for arm in ARMS:
        counts = {k: sum(r["arms"][arm][k] for r in rows) for k in ("tp", "fp", "fn")}
        tp, fp, fn = (counts[k] for k in ("tp", "fp", "fn"))
        ranks = [next((1/rank for rank, source in enumerate(r["arms"][arm]["sources"], 1)
                       if source in r["expected_sources"]), 0) for r in positives]
        result["arms"][arm] = {**counts, "recall_at_3": tp/(tp+fn) if tp+fn else None,
            "precision_at_3": tp/(tp+fp) if tp+fp else None,
            "mrr_at_3": sum(ranks)/len(ranks) if ranks else None,
            "false_positive_queries_with_empty_gold": sum(r["arms"][arm]["false_positive_on_empty_gold"] for r in rows),
            "latency": latency_summary([ms for r in rows for ms in r["arms"][arm]["samples_ms"]]),
            "latency_by_candidate_presence": {label: latency_summary([ms for r in rows
                if bool(r["eligible_candidates"]) == present for ms in r["arms"][arm]["samples_ms"]])
                for label, present in (("nonempty", True), ("empty", False))}}
    return result


class SyntheticVectors:
    """1024-D storage workload, NOT semantic model/quality evidence."""
    model_fingerprint = "codecrew-qdrant-scale-v1-1024"
    dimensions = 1024

    def __init__(self):
        self.rng, self.calls = random.Random(1729), 0

    def embed(self, texts):
        self.calls += len(texts)
        result = []
        for text in texts:
            if text in ("gateway 超时", "后台任务怎么解耦") or text.startswith("gateway 决策"):
                vector = (1.,) + (0.,) * (self.dimensions-1)
            elif text == "没有相关的词项":
                vector = (0., 1.) + (0.,) * (self.dimensions-2)
            else:
                # Orthogonal distractors keep ties/threshold drift out of perf measurement.
                vector = (0., 0.) + tuple(self.rng.uniform(-1., 1.) for _ in range(self.dimensions-2))
            result.append(vector)
        return tuple(result)


def scale_suite(pair, path, backend, endpoint, *, repeats, warmups, read_runner=None):
    candidates, total = pair
    store = StandaloneChatStore(SQLiteDatabase(path), lexical_backend="bm25")
    store.initialize()
    service = StandaloneChatService(store)
    worker = MemoryVectorOutbox(store.database, backend)
    worker.register_target()
    embeddings = SyntheticVectors()
    reference = MemoryVectorIndex(store.database, embeddings)
    by_source, target = {}, None
    remaining = total - candidates
    sizes = [candidates] + [min(200, remaining-i) for i in range(0, remaining, 200)]
    offset = 0
    for count in sizes:
        room = service.create_room(title=f"synthetic scale {offset}", idempotency_key=uuid4()).room_id
        if offset == 0:
            target = room
        for i in range(count):
            text = "gateway 决策读超时 8 秒" if i == 0 else f"审计归档样本 {offset+i}"
            source = service.post_message(room, content="@白金 " + text, idempotency_key=uuid4(), reply_to=None).message
            memory = service.create_memory(room, source_message_id=source.message_id, content=text)
            reference.index((memory,))
            if room == target:
                by_source[source.message_id] = "target" if i == 0 else f"noise-{i}"
        offset += count
    require(sum(1 for _ in by_source) == candidates, "scale_candidate_count_changed")
    with store.database.connect() as conn:
        actual = conn.execute("SELECT count(*) FROM standalone_chat_memories").fetchone()[0]
    require(actual == total, "scale_global_count_changed")
    before = canonical_snapshot(store)
    drain(worker)
    initial_audit = finish_audit(worker)
    indexes = prepared_indexes(store, embeddings, backend, endpoint)
    meter = RemoteMeter(backend)
    discussion = uuid4()
    rows = []
    for i, query in enumerate(("gateway 超时", "后台任务怎么解耦", "没有相关的词项")):
        expected = ["target"] if i < 2 else []
        parity = storage_parity(store, target, discussion, query, embeddings, worker)
        prior = meter.calls
        arms = (read_runner or paired_reads)(store, target, discussion, query, by_source, expected, indexes,
                            repeats=repeats, warmups=warmups, offset=i)
        rows.append({"case_id": f"synthetic-{i}", "domain": "synthetic_storage_not_model_quality",
                     "expected_sources": expected, "eligible_candidates": candidates, "arms": arms,
                     "storage_parity": parity, "read_http_requests_including_warmups": meter.calls-prior})
    require(canonical_snapshot(store) == before, "scale_canonical_changed")
    summary = summarize(rows)
    summary["paired_storage"] = paired_summary(rows)
    return {"candidates_per_query": candidates, "global_points": total, "dimensions": embeddings.dimensions,
            "rows": rows, "summary": summary, "initial_audit": initial_audit,
            "outbox_status": worker.status(), "sqlite_file_bytes": path.stat().st_size,
            "synthetic_vector_texts": embeddings.calls}


def run_suite(*, cache, repeats=20, warmups=3, scales=SCALES, output_root=RESULTS):
    bounds(repeats, warmups, scales)
    cases = validate_unseen()
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    _require_review(lock)
    verify_frozen(measure_legacy(cases), json.loads(FROZEN_PATH.read_text(encoding="utf-8")))
    embeddings, identity = SharedEmbeddings.load(cache)
    require(embeddings.dimensions is not None, "empty_quality_cache")
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = OwnedDocker(output / "qdrant-storage")
    manifest = {"complete": False, "cleanup_complete": False, "embedding_calls": 0, "agent_calls": 0,
                "default_changed": False, "image": IMAGE, "quality_model": identity,
                "quality_fingerprint": embeddings.model_fingerprint, "dimensions": embeddings.dimensions,
                "dataset_sha256": _sha256(CASES_PATH), "label_lock_sha256": _sha256(LOCK_PATH),
                "cache_input_sha256": _sha256(cache), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "fts_revision": FTS_TEXT_REVISION,
                "parameters": {"repeats": repeats, "warmups": warmups, "scales": scales, "arms": ARMS,
                               "candidate_limit": 200, "limit": 3, "rrf": 60, "threshold": THRESHOLD, "exact": True},
                "timing_scope": "warm retrieve, cached/synthetic query vectors, loopback HTTP included; "
                    "alternated arm order; no cloud, startup, indexing, delivery, audit or Agent time",
                "quality_scope": "24 already-seen human-reviewed regression cases, gold fact seeding, cached cloud vectors; not new blind eval",
                "scale_scope": "synthetic storage workload; bounded 200 candidate exact search, no ANN/SLA/concurrency claims",
                "code_sha256": {p: _sha256(ROOT / p) for p in ("evals/benchmark_memory_qdrant.py",
                    "evals/check_memory_qdrant_faults.py", "app/chat/qdrant_memory_index.py", "app/chat/vector_memory.py",
                    "app/chat/qdrant_vector_sync.py", "app/chat/qdrant_vector_backend.py", "app/chat/memory.py")}}
    def checkpoint():
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint()
    try:
        control.create()
        control.start()
        manifest["image_id"], manifest["container_name"] = control.image_id, control.name
        endpoint = QdrantEndpoint(base_url=control.url, collection_prefix="codecrew_quality", timeout_seconds=5)
        backend = QdrantVersionedVectorBackend(endpoint, VectorModel(embeddings.model_fingerprint, embeddings.dimensions))
        backend.initialize()
        rows, quality = quality_suite(cases, output / "quality.sqlite3", embeddings, backend, endpoint,
                                     repeats=repeats, warmups=warmups)
        (output / "quality.json").write_text(json.dumps({"rows": rows, "summary": quality}, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint()
        scale_results = []
        for i, pair in enumerate(scales):
            print(f"[scale {i+1}/{len(scales)}] candidates={pair[0]} global={pair[1]}", flush=True)
            endpoint = QdrantEndpoint(base_url=control.url, collection_prefix=f"codecrew_scale_{i}", timeout_seconds=5)
            backend = QdrantVersionedVectorBackend(endpoint, VectorModel(SyntheticVectors.model_fingerprint, 1024))
            backend.initialize()
            result = scale_suite(pair, output / f"scale-{i}.sqlite3", backend, endpoint, repeats=repeats, warmups=warmups)
            scale_results.append(result)
            (output / f"scale-{i}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            checkpoint()
        summary = {"quality": quality, "scales": [{k: v for k, v in row.items() if k != "rows"} for row in scale_results],
                   "embedding_calls": 0, "agent_calls": 0, "billing_cost": None, "default_changed": False}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["measurements_complete"] = True
    except Exception as exc:  # noqa: BLE001 - provider/cache/Docker failures are NOT quality misses
        manifest["error_type"] = type(exc).__name__
    finally:
        try:
            manifest["cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - refuse foreign resource deletion
            manifest["cleanup_error_type"] = type(exc).__name__
        manifest["complete"] = bool(manifest.get("measurements_complete") and manifest["cleanup_complete"])
        manifest["embedding_errors"] = embeddings.errors
        checkpoint()
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: --live --cache replays 24 cached-cloud cases and synthetic 10/50/200-candidate scales "
              "in one owned Qdrant Docker container, then removes only that container; retains evidence. "
              "No settings/cloud/Agent/real chat/default change; exact, not ANN.")
        return
    if args.cache is None:
        parser.error("--live requires a complete --cache (no implicit cloud collection)")
    try:
        output, manifest = run_suite(cache=args.cache, repeats=args.repeats, warmups=args.warmups)
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"Benchmark preflight failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not manifest["complete"]:
        parser.exit(1, "Benchmark incomplete; not scored as memory misses\n")
    print("Paired cached quality and synthetic exact-scale benchmark complete; owned cleanup passed; models 0/0")


if __name__ == "__main__":
    main()
