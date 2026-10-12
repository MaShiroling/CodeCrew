"""Same-run factorial read-shape A/B; cached fixtures, no paid model calls.

Controls share current validators, not byte-identical historical deployments.
Official timings have no Capture instrumentation. Diagnostic counts are separate.
Default is plan only; --live owns/cleans a new Docker, never the existing service.
"""

import argparse
import json
import math
import platform
import random
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from evals import benchmark_memory_qdrant as bench
from evals.profile_memory_qdrant import PROVENANCE, Capture
from evals.qdrant_readiness_reference import RepeatedReadinessBackend
from evals.qdrant_snapshot_reference import rowwise_snapshot

RESULTS = bench.ROOT / "evals/results/memory-qdrant-optimization-ab-v1"
REMOTE_ARMS = ("qdrant_old_shape", "qdrant_batch_only", "qdrant_readiness_only", "qdrant_hybrid")
ARMS = ("bm25", "sqlite_hybrid", *REMOTE_ARMS)
COMPARISONS = (("qdrant_old_shape", "qdrant_hybrid"),
               ("qdrant_old_shape", "qdrant_batch_only"),
               ("qdrant_old_shape", "qdrant_readiness_only"),
               ("qdrant_batch_only", "qdrant_hybrid"),
               ("qdrant_readiness_only", "qdrant_hybrid"),
               ("sqlite_hybrid", "qdrant_hybrid"))


class RowwiseIndex(QdrantMemoryVectorIndex):
    """Fixture-only subclass; no production setting or class patch."""
    def _snapshot(self, conn, memories, **scope):
        return rowwise_snapshot(self, conn, memories, **scope)


def arm_order(iteration, offset=0):
    # Six cyclic rotations followed by their reversals: every arm gets every
    # position twice per 12 rounds. Preserve each round's paired sample index.
    step = (iteration + offset) % (2 * len(ARMS))
    rotation = step % len(ARMS)
    order = ARMS[rotation:] + ARMS[:rotation]
    return order if step < len(ARMS) else tuple(reversed(order))


def factorial_indexes(indexes, backend):
    current = indexes["qdrant_hybrid"].inner
    repeated = RepeatedReadinessBackend(backend.endpoint, backend.model)
    repeated._opener = backend._opener  # Same transport, model, collection and points.
    bench.require(repeated.collection_name == backend.collection_name and repeated.sync_target_id == backend.sync_target_id,
                  "control_namespace_changed")
    repeated._request = backend._request  # Same bounded HTTP implementation and fixture request meter.
    result = dict(indexes)
    backends = {}
    for arm in REMOTE_ARMS:
        remote = repeated if arm in ("qdrant_old_shape", "qdrant_batch_only") else backend
        kind = RowwiseIndex if arm in ("qdrant_old_shape", "qdrant_readiness_only") else QdrantMemoryVectorIndex
        result[arm] = bench.ObservedIndex(kind(current.database, current.embedder, backend.endpoint,
            min_similarity=current.min_similarity, backend_factory=lambda model, remote=remote: remote))
        backends[arm] = remote
    return result, backends


def source_ids(memories, by_source):
    return [by_source[m.source_message_id] for m in memories]


def factorial_reads(store, room, discussion, query, by_source, expected, indexes, *, repeats, warmups, offset=0):
    current = indexes["qdrant_hybrid"].inner
    with store.database.connect() as conn:
        meta = conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                            (current.embedder.model_fingerprint,)).fetchone()
    backend = current._backend(bench.VectorModel(current.embedder.model_fingerprint, meta[0] if meta else 2))
    indexes, backends = factorial_indexes(indexes, backend)
    # Independent full semantic ranking before timed retrieval, not only final RRF top 3.
    candidates = store.memory.list_active(room)[:200]
    ranks = {}
    normalized = bench._MENTION.sub("", query).strip()
    for arm in REMOTE_ARMS:
        ranks[arm] = indexes[arm].search_scoped(normalized, candidates, room_id=room,
                                               exclude_correlation_id=discussion)
    bench.require(all(ranks[a] == ranks["qdrant_hybrid"] for a in REMOTE_ARMS), "control_full_rank_changed")
    diagnostics, outputs = {}, {}
    for arm in REMOTE_ARMS:
        with Capture(store, indexes[arm].inner, backends[arm]) as capture:
            memories, _ = bench.measured_recall(store, room, discussion, query, indexes[arm])
        outputs[arm] = source_ids(memories, by_source)
        diagnostics[arm] = capture.sample()
        if diagnostics[arm]["calls"].get("http_query"):
            gets = 5 if arm in ("qdrant_old_shape", "qdrant_batch_only") else 2
            bench.require(diagnostics[arm]["calls"].get("http_metadata") == gets
                          and diagnostics[arm]["calls"].get("http_query") == 1, "metadata_shape_changed")
    samples = {a: [] for a in ARMS}
    orders = []
    for iteration in range(warmups + repeats):
        order = arm_order(iteration, offset)
        for arm in order:
            memories, ms = bench.measured_recall(store, room, discussion, query, indexes.get(arm))
            sources = source_ids(memories, by_source)
            bench.require(arm not in outputs or sources == outputs[arm], "unstable_ordered_sources")
            outputs[arm] = sources
            if iteration >= warmups:
                samples[arm].append(ms)
        bench.require(all(outputs[a] == outputs["qdrant_hybrid"] for a in REMOTE_ARMS), "control_sources_changed")
        if iteration >= warmups:
            orders.append(order)
    result = {a: {"sources": outputs[a], **bench._scores(set(expected), outputs[a]),
                  "samples_ms": samples[a], "latency": bench.latency_summary(samples[a])} for a in ARMS}
    for arm in REMOTE_ARMS:
        result[arm]["diagnostic"] = diagnostics[arm]
        result[arm]["full_rank_equal"] = True
        result[arm]["full_rank_count"] = len(ranks[arm])
    # Shared round order stored once, separate from latency arrays.
    result["qdrant_hybrid"]["measured_round_orders"] = orders
    return result


def paired_effect(rows, control, candidate, *, draws=2000):
    """Paired delta, with query-cluster bootstrap (NOT independent-call CI).

    Equal repeats required. Resample whole queries, preserving all matched rounds.
    Scales have only three fixed queries: their CI is descriptive, not an SLA.
    """
    if not rows or type(draws) is not int or not 1 <= draws <= 10000:
        raise ValueError("bounded nonempty paired rows required")
    clusters, pairs, repeats = [], [], None
    for row in rows:
        a, b = (row["arms"][arm]["samples_ms"] for arm in (control, candidate))
        if not a or len(a) != len(b) or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in (*a, *b)):
            raise ValueError("positive finite paired samples required")
        if repeats is not None and len(a) != repeats:
            raise ValueError("equal repeats per query required")
        repeats = len(a)
        pairs.extend(zip(a, b, strict=True))
        clusters.append(statistics.mean(x-y for x, y in zip(a, b, strict=True)))
    rng = random.Random(1729)
    bootstrap = sorted(statistics.mean(rng.choices(clusters, k=len(clusters))) for _ in range(draws))
    mean_control = statistics.mean(a for a, _ in pairs)
    mean_candidate = statistics.mean(b for _, b in pairs)
    return {"control": control, "candidate": candidate, "pairs": len(pairs), "query_clusters": len(clusters),
            "mean_control_ms": mean_control, "mean_candidate_ms": mean_candidate,
            "mean_saved_ms": mean_control-mean_candidate,
            "median_paired_saved_ms": statistics.median(a-b for a, b in pairs),
            "mean_reduction_pct": 100*(mean_control-mean_candidate)/mean_control,
            "candidate_faster_fraction": sum(b < a for a, b in pairs)/len(pairs),
            "query_cluster_mean_saved_ci95_ms": [bootstrap[int(.025*(draws-1))], bootstrap[int(.975*(draws-1))]],
            "bootstrap_draws": draws, "bootstrap_seed": 1729}


def summarize(rows):
    # Empty canonical candidate cases short circuit and must not dilute remote-call latency.
    nonempty = [r for r in rows if r["arms"]["qdrant_hybrid"]["diagnostic"]["calls"].get("http_query")]
    empty = [r for r in rows if r not in nonempty]
    totals = {a: {k: sum(r["arms"][a][k] for r in rows) for k in ("tp", "fp", "fn")} for a in ARMS}
    return {"case_count": len(rows), "nonempty_remote_cases": len(nonempty), "short_circuit_cases": len(empty),
            "quality_totals": totals,
            "all_control_sources_equal": all(r["arms"][a]["sources"] == r["arms"]["qdrant_hybrid"]["sources"]
                                             for r in rows for a in REMOTE_ARMS),
            "latencies_nonempty": {a: bench.latency_summary([v for r in nonempty for v in r["arms"][a]["samples_ms"]]) for a in ARMS},
            "latencies_short_circuit": {a: bench.latency_summary([v for r in empty for v in r["arms"][a]["samples_ms"]]) for a in ARMS},
            "paired_nonempty": [paired_effect(nonempty, a, b) for a, b in COMPARISONS] if nonempty else [],
            "uncertainty_scope": "query-cluster bootstrap; seen fixed fixtures, serial warm loopback, not population/production SLA"}


def check_scale_counts(rows, candidates):
    for row in rows:
        for arm in REMOTE_ARMS:
            diagnostic = row["arms"][arm]["diagnostic"]
            old_sql = arm in ("qdrant_old_shape", "qdrant_readiness_only")
            expected = 3*candidates+2 if old_sql else 5
            for phase in ("canonical_pre_snapshot", "canonical_post_snapshot"):
                bench.require(diagnostic["visible_sql"].get(phase, {}).get("SELECT", 0) == expected, "snapshot_shape_changed")
            gets = 5 if arm in ("qdrant_old_shape", "qdrant_batch_only") else 2
            bench.require(diagnostic["calls"].get("http_metadata") == gets and diagnostic["calls"].get("http_query") == 1,
                          "metadata_shape_changed")


def run_suite(*, cache, repeats=24, warmups=3, scales=bench.SCALES, output_root=RESULTS):
    bench.bounds(repeats, warmups, scales)
    cases = bench.validate_unseen()
    bench._require_review(json.loads(bench.LOCK_PATH.read_text(encoding="utf-8")))
    bench.verify_frozen(bench.measure_legacy(cases), json.loads(bench.FROZEN_PATH.read_text(encoding="utf-8")))
    embeddings, identity = bench.SharedEmbeddings.load(cache)
    bench.require(embeddings.dimensions is not None, "empty_quality_cache")
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = bench.OwnedDocker(output / "qdrant-storage")
    provenance = (*PROVENANCE, "evals/benchmark_memory_qdrant_optimization.py",
                  "evals/qdrant_snapshot_reference.py", "evals/qdrant_readiness_reference.py")
    manifest = {"complete": False, "cleanup_complete": False, "embedding_calls": 0, "agent_calls": 0,
                "default_changed": False, "image": bench.IMAGE, "quality_model": identity,
                "quality_fingerprint": embeddings.model_fingerprint, "dimensions": embeddings.dimensions,
                "dataset_sha256": bench._sha256(bench.CASES_PATH), "label_lock_sha256": bench._sha256(bench.LOCK_PATH),
                "cache_input_sha256": bench._sha256(cache), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "parameters": {"repeats": repeats, "warmups": warmups, "arms": ARMS,
                    "scales": scales, "candidate_limit": 200, "limit": 3, "rrf": 60, "threshold": bench.THRESHOLD, "exact": True},
                "control_scope": "eval-only old read shapes with CURRENT shared validators; not historical executable",
                "timing_scope": "uninstrumented warm serial retrieve, same canonical/cache/points/owned server; rotations+reversals; matched round arrays",
                "diagnostic_scope": "one separately instrumented call per Qdrant arm/query; SQL counts not SQL durations",
                "quality_scope": "24 already-seen reviewed gold-seeded cases; cached cloud vectors; not blind extraction/answer eval",
                "scale_scope": "synthetic 1024-D exact scoped search, <=200 candidates; not ANN/concurrent SLA",
                "code_sha256": {p: bench._sha256(bench.ROOT / p) for p in provenance}}
    def write(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    write("manifest.json", manifest)
    try:
        control.create()
        control.start()
        manifest["image_id"], manifest["container_name"] = control.image_id, control.name
        endpoint = bench.QdrantEndpoint(base_url=control.url, collection_prefix="codecrew_opt_quality", timeout_seconds=5)
        backend = bench.QdrantVersionedVectorBackend(endpoint, bench.VectorModel(embeddings.model_fingerprint, embeddings.dimensions))
        backend.initialize()
        rows, base = bench.quality_suite(cases, output / "quality.sqlite3", embeddings, backend, endpoint,
            repeats=repeats, warmups=warmups, read_runner=factorial_reads)
        bench.require(all(r["storage_parity"]["ordered_ids_equal"] and r["storage_parity"]["max_score_delta"] < 1e-5 for r in rows),
                      "storage_parity_changed")
        quality = {**summarize(rows), "base_summary": base}
        write("quality.json", {"rows": rows, "summary": quality})
        scale_results = []
        for i, pair in enumerate(scales):
            print(f"[scale {i+1}/{len(scales)}] candidates={pair[0]} global={pair[1]}", flush=True)
            endpoint = bench.QdrantEndpoint(base_url=control.url, collection_prefix=f"codecrew_opt_scale_{i}", timeout_seconds=5)
            backend = bench.QdrantVersionedVectorBackend(endpoint, bench.VectorModel(bench.SyntheticVectors.model_fingerprint, 1024))
            backend.initialize()
            result = bench.scale_suite(pair, output / f"scale-{i}.sqlite3", backend, endpoint,
                repeats=repeats, warmups=warmups, read_runner=factorial_reads)
            check_scale_counts(result["rows"], pair[0])
            bench.require(all(r["storage_parity"]["ordered_ids_equal"] and r["storage_parity"]["max_score_delta"] < 1e-5
                              for r in result["rows"]), "scale_storage_parity_changed")
            result["factorial_summary"] = summarize(result["rows"])
            scale_results.append({k: v for k, v in result.items() if k != "rows"})
            write(f"scale-{i}.json", result)
        bench.require(all(bench._sha256(bench.ROOT / p) == sha for p, sha in manifest["code_sha256"].items()), "code_changed_during_run")
        bench.require(embeddings.calls == embeddings.errors == 0, "provider_or_cache_error")
        write("summary.json", {"quality": quality, "scales": scale_results, "embedding_calls": 0, "agent_calls": 0,
                               "billing_cost": None, "default_changed": False})
        manifest["measurements_complete"] = True
    except Exception as exc:  # noqa: BLE001 - no raw provider/body/credential logging
        manifest["error_type"] = type(exc).__name__
    finally:
        try:
            manifest["cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - ownership failure must not delete foreign resources
            manifest["cleanup_error_type"] = type(exc).__name__
        manifest["embedding_errors"] = embeddings.errors
        manifest["complete"] = bool(manifest.get("measurements_complete") and manifest["cleanup_complete"])
        write("manifest.json", manifest)
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--repeats", type=int, default=24)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: cached 24-case + four-scale same-run 2x2 read-shape A/B, six arms, "
              "24 paired rounds/3 warmups, new owned Docker only. No settings/cloud/Agent/default change.")
        return
    if args.cache is None:
        parser.error("--live requires a complete --cache; no implicit cloud collection")
    try:
        output, manifest = run_suite(cache=args.cache, repeats=args.repeats, warmups=args.warmups)
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"A/B preflight failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not manifest["complete"]:
        parser.exit(1, "A/B incomplete; not scored as memory misses\n")
    print("Same-run factorial A/B complete; owned cleanup passed; models 0/0; default unchanged")


if __name__ == "__main__":
    main()
