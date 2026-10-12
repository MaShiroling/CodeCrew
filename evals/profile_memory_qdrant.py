"""Read-only, isolated Qdrant profiling. Plan only unless --live --cache.

Nested exclusive wall times partition retrieve; inclusive times must NOT be
added. SQL trace counts (not SQL timings) omit connection-opening PRAGMAs.
HTTP wall time includes serialization/network/server/decoding, not engine time.
No SQL/text/vector/credential payloads are retained in diagnostic samples.
"""

import argparse
import json
import time
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.qdrant_memory_index import SNAPSHOT_READ_REVISION
from app.chat.qdrant_vector_sync import READINESS_CHECK_REVISION
from evals import benchmark_memory_qdrant as bench

RESULTS = bench.ROOT / "evals/results/memory-qdrant-profile-v1"
ARMS = (*bench.ARMS, "qdrant_profiled")
PHASES = ("retrieve_other", "candidate_snapshot", "bm25_search", "index_orchestration",
          "canonical_pre_snapshot", "canonical_post_snapshot", "cached_query_vector",
          "remote_ready_guard", "remote_search_local", "http_metadata", "http_query",
          "final_canonical_revalidation")
PROVENANCE = ("evals/profile_memory_qdrant.py", "evals/benchmark_memory_qdrant.py",
              "evals/check_memory_qdrant_faults.py", "evals/run_memory_fts_ab.py",
              "evals/run_memory_retrieval_ab.py", "app/chat/memory.py", "app/chat/fts_memory.py",
              "app/chat/fts_text.py", "app/chat/qdrant_memory_index.py", "app/chat/qdrant_vector_sync.py",
              "app/chat/qdrant_vector_backend.py", "app/chat/vector_outbox.py", "app/chat/vector_memory.py",
              "app/chat/vector_contracts.py", "app/chat/sqlite_vector_backend.py", "app/storage/sqlite.py")


class Spans:
    """Nested spans with exclusive, additive nanoseconds; injectable test clock."""
    def __init__(self, clock=time.perf_counter_ns):
        self.clock, self.stack = clock, []
        self.exclusive, self.inclusive, self.calls = Counter(), Counter(), Counter()

    @contextmanager
    def span(self, name):
        if name not in PHASES:
            raise ValueError("unknown diagnostic phase")
        now = self.clock()
        if self.stack:
            parent = self.stack[-1]
            self.exclusive[parent[0]] += now - parent[2]
        frame = [name, now, now]
        self.stack.append(frame)
        try:
            yield
        finally:
            end = self.clock()
            self.exclusive[name] += end - frame[2]
            self.inclusive[name] += end - frame[1]
            self.calls[name] += 1
            self.stack.pop()
            if self.stack:
                self.stack[-1][2] = end


class Capture:
    """Temporary patches on fixture instances only; no production/class patches.

    Single-threaded diagnostic harness, NOT application telemetry. Installation,
    restoration and report aggregation are outside measured retrieve calls.
    """
    def __init__(self, store, index, backend):
        self.store, self.index, self.backend = store, index, backend
        self.spans = Spans()
        self.sql = defaultdict(Counter)
        self.restores, self.active, self.snapshot_count = [], False, 0

    def patch(self, obj, name, replacement):
        present, value = name in vars(obj), vars(obj).get(name)
        self.restores.append((obj, name, present, value))
        setattr(obj, name, replacement)

    def wrap(self, obj, name, phase):
        original = getattr(obj, name)
        def measured(*args, **kwargs):
            with self.spans.span(phase) if self.active else nullcontext():
                return original(*args, **kwargs)
        self.patch(obj, name, measured)

    def __enter__(self):
        if self.restores:
            raise RuntimeError("capture already installed")
        try:
            original = self.store.memory.retrieve
            def retrieve(*args, **kwargs):
                if self.active or self.spans.calls["retrieve_other"]:
                    raise RuntimeError("capture requires one nonreentrant retrieve")
                self.active = True
                try:
                    with self.spans.span("retrieve_other"):
                        return original(*args, **kwargs)
                finally:
                    self.active = False
            self.patch(self.store.memory, "retrieve", retrieve)
            self.wrap(self.store.memory.fts_index, "search", "bm25_search")
            self.wrap(self.store.memory, "_validate_recall", "final_canonical_revalidation")
            self.wrap(self.index, "search_scoped", "index_orchestration")
            self.wrap(self.index.embedder, "embed", "cached_query_vector")
            self.wrap(self.backend, "_require_sync_ready", "remote_ready_guard")
            self.wrap(self.backend, "search_current", "remote_search_local")
            snapshot = self.index._snapshot
            def measured_snapshot(*args, **kwargs):
                if not self.active:
                    return snapshot(*args, **kwargs)
                self.snapshot_count += 1
                bench.require(self.snapshot_count <= 2, "snapshot_contract_changed")
                phase = "canonical_pre_snapshot" if self.snapshot_count == 1 else "canonical_post_snapshot"
                with self.spans.span(phase):
                    return snapshot(*args, **kwargs)
            self.patch(self.index, "_snapshot", measured_snapshot)
            request = self.backend._request
            def measured_request(method, path, *args, **kwargs):
                if not self.active:
                    return request(method, path, *args, **kwargs)
                if method == "GET" and path == self.backend._path:
                    phase = "http_metadata"
                elif method == "POST" and path == self.backend._path + "/points/query":
                    phase = "http_query"
                else:
                    raise RuntimeError("unexpected diagnostic read endpoint")
                with self.spans.span(phase):
                    return request(method, path, *args, **kwargs)
            self.patch(self.backend, "_request", measured_request)
            connect = self.store.database.connect
            def trace(statement):
                # Keep only bounded categories. SQLite may expand parameters here:
                # never store, log or propagate the raw statement.
                token = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
                category = token if token in ("SELECT", "BEGIN", "COMMIT", "ROLLBACK") else "OTHER"
                self.sql[self.spans.stack[-1][0]][category] += 1
            @contextmanager
            def measured_connect():
                if not self.active:
                    with connect() as conn:
                        yield conn
                    return
                phase = (self.spans.span("candidate_snapshot")
                         if self.spans.stack[-1][0] == "retrieve_other" else nullcontext())
                with phase, connect() as conn:
                    conn.set_trace_callback(trace)
                    try:
                        yield conn
                    finally:
                        conn.set_trace_callback(None)
            self.patch(self.store.database, "connect", measured_connect)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        for obj, name, present, value in reversed(self.restores):
            if present:
                setattr(obj, name, value)
            else:
                delattr(obj, name)
        self.restores.clear()

    def sample(self):
        bench.require(not self.active and not self.spans.stack and self.spans.calls["retrieve_other"] == 1,
                      "incomplete_profile")
        total = self.spans.inclusive["retrieve_other"]
        bench.require(sum(self.spans.exclusive.values()) == total, "nonadditive_profile")
        return {"root_ms": total / 1e6,
                "exclusive_ms": {p: self.spans.exclusive[p] / 1e6 for p in PHASES},
                "inclusive_ms": {p: self.spans.inclusive[p] / 1e6 for p in PHASES},
                "calls": dict(self.spans.calls), "visible_sql": dict(self.sql)}


def profiled_reads(store, room, discussion, query, by_source, expected, indexes, *, repeats, warmups, offset=0):
    remote = indexes["qdrant_hybrid"].inner
    with store.database.connect() as conn:
        dimensions = conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                                  (remote.embedder.model_fingerprint,)).fetchone()
    backend = remote._backend(bench.VectorModel(remote.embedder.model_fingerprint, dimensions[0] if dimensions else 2))
    outputs, samples, profiles = {}, {a: [] for a in ARMS}, []
    for iteration in range(warmups + repeats):
        order = ARMS if (iteration + offset) % 2 == 0 else tuple(reversed(ARMS))
        for arm in order:
            capture = Capture(store, remote, backend) if arm == "qdrant_profiled" else None
            with capture if capture else nullcontext():
                memories, ms = bench.measured_recall(store, room, discussion, query,
                    indexes.get("qdrant_hybrid" if capture else arm))
            sources = [by_source[m.source_message_id] for m in memories]
            bench.require(arm not in outputs or sources == outputs[arm], "unstable_ordered_sources")
            outputs[arm] = sources
            if iteration >= warmups:
                samples[arm].append(ms)
                if capture:
                    profiles.append(capture.sample())
        bench.require(outputs["qdrant_profiled"] == outputs["qdrant_hybrid"], "instrumentation_changed_sources")
    result = {a: {"sources": outputs[a], **bench._scores(set(expected), outputs[a]),
                  "samples_ms": samples[a], "latency": bench.latency_summary(samples[a])} for a in ARMS}
    result["qdrant_profiled"]["profiles"] = profiles
    return result


def profile_summary(rows):
    samples = [p for r in rows for p in r["arms"]["qdrant_profiled"]["profiles"]]
    baseline = [ms for r in rows for ms in r["arms"]["qdrant_hybrid"]["samples_ms"]]
    instrumented = [ms for r in rows for ms in r["arms"]["qdrant_profiled"]["samples_ms"]]
    total = sum(s["root_ms"] for s in samples)
    phases = {phase: {"exclusive_latency": bench.latency_summary([s["exclusive_ms"][phase] for s in samples]),
                     "inclusive_latency": bench.latency_summary([s["inclusive_ms"][phase] for s in samples]),
                     "exclusive_share": sum(s["exclusive_ms"][phase] for s in samples) / total if total else 0,
                     "calls_per_retrieve": sorted({s["calls"].get(phase, 0) for s in samples}),
                     "visible_sql_counts_per_retrieve": {kind: sorted({s["visible_sql"].get(phase, {}).get(kind, 0)
                         for s in samples}) for kind in ("SELECT", "BEGIN", "COMMIT", "ROLLBACK", "OTHER")}}
              for phase in PHASES}
    return {"samples": len(samples), "unprofiled_latency": bench.latency_summary(baseline),
            "profiled_latency": bench.latency_summary(instrumented),
            "profiled_to_unprofiled_mean_ratio": sum(instrumented) / sum(baseline) if sum(baseline) else None,
            "ordered_sources_equal": all(r["arms"]["qdrant_profiled"]["sources"] ==
                                         r["arms"]["qdrant_hybrid"]["sources"] for r in rows),
            "phases": phases}


def run_suite(*, cache, repeats=20, warmups=3, scales=bench.SCALES, output_root=RESULTS):
    bench.bounds(repeats, warmups, scales)
    cases = bench.validate_unseen()
    bench._require_review(json.loads(bench.LOCK_PATH.read_text(encoding="utf-8")))
    bench.verify_frozen(bench.measure_legacy(cases), json.loads(bench.FROZEN_PATH.read_text(encoding="utf-8")))
    embeddings, identity = bench.SharedEmbeddings.load(cache)
    bench.require(embeddings.dimensions is not None, "empty_quality_cache")
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = bench.OwnedDocker(output / "qdrant-storage")
    manifest = {"complete": False, "cleanup_complete": False, "optimization_applied": True,
                "read_path_revision": SNAPSHOT_READ_REVISION,
                "remote_readiness_revision": READINESS_CHECK_REVISION,
                "default_changed": False, "embedding_calls": 0, "agent_calls": 0, "image": bench.IMAGE,
                "quality_model": identity, "quality_fingerprint": embeddings.model_fingerprint,
                "dimensions": embeddings.dimensions, "dataset_sha256": bench._sha256(bench.CASES_PATH),
                "label_lock_sha256": bench._sha256(bench.LOCK_PATH), "cache_input_sha256": bench._sha256(cache),
                "python": bench.platform.python_version(), "sqlite": bench.sqlite3.sqlite_version,
                "platform": bench.platform.platform(), "fts_revision": bench.FTS_TEXT_REVISION,
                "parameters": {"repeats": repeats, "warmups": warmups, "scales": scales, "arms": ARMS,
                               "threshold": bench.THRESHOLD, "candidate_limit": 200, "exact": True},
                "timing_scope": "nested exclusive client wall spans partition retrieve; inclusive spans overlap; "
                    "SQL trace counts omit connection-opening PRAGMAs; HTTP includes JSON/network/server/decoding; "
                    "cached query vectors; instrumentation perturbation measured with alternating fourth arm",
                "quality_scope": "24 already-seen human-reviewed gold-seeded cases, not blind eval or live Agent",
                "scale_scope": "synthetic 1024D bounded candidate exact search, not semantic model/ANN/SLA",
                "code_sha256": {p: bench._sha256(bench.ROOT / p) for p in PROVENANCE}}
    def save(name, data):
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    save("manifest.json", manifest)
    try:
        control.create()
        control.start()
        manifest["image_id"], manifest["container_name"] = control.image_id, control.name
        endpoint = bench.QdrantEndpoint(base_url=control.url, collection_prefix="codecrew_profile_quality", timeout_seconds=5)
        backend = bench.QdrantVersionedVectorBackend(endpoint, bench.VectorModel(embeddings.model_fingerprint, embeddings.dimensions))
        backend.initialize()
        rows, summary = bench.quality_suite(cases, output / "quality.sqlite3", embeddings, backend, endpoint,
            repeats=repeats, warmups=warmups, read_runner=profiled_reads)
        quality = {"benchmark": summary, "all": profile_summary(rows), "by_candidate_presence": {
            name: profile_summary([r for r in rows if bool(r["eligible_candidates"]) == present])
            for name, present in (("nonempty", True), ("empty", False))}}
        save("quality.json", {"rows": rows, "summary": quality})
        save("manifest.json", manifest)
        scale_results = []
        for i, pair in enumerate(scales):
            print(f"[profile scale {i+1}/{len(scales)}] candidates={pair[0]} global={pair[1]}", flush=True)
            endpoint = bench.QdrantEndpoint(base_url=control.url, collection_prefix=f"codecrew_profile_scale_{i}", timeout_seconds=5)
            backend = bench.QdrantVersionedVectorBackend(endpoint, bench.VectorModel(bench.SyntheticVectors.model_fingerprint, 1024))
            backend.initialize()
            result = bench.scale_suite(pair, output / f"scale-{i}.sqlite3", backend, endpoint,
                repeats=repeats, warmups=warmups, read_runner=profiled_reads)
            result["profile"] = profile_summary(result["rows"])
            scale_results.append({k: v for k, v in result.items() if k != "rows"})
            save(f"scale-{i}.json", result)
            save("manifest.json", manifest)
        save("summary.json", {"quality": quality, "scales": scale_results, "embedding_calls": 0,
                              "agent_calls": 0, "default_changed": False, "optimization_applied": True,
                              "read_path_revision": SNAPSHOT_READ_REVISION,
                              "remote_readiness_revision": READINESS_CHECK_REVISION})
        manifest["measurements_complete"] = True
    except Exception as exc:  # noqa: BLE001 - error category only, not a memory miss
        manifest["error_type"] = type(exc).__name__
    finally:
        try:
            manifest["cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - ownership failure forbids foreign deletion
            manifest["cleanup_error_type"] = type(exc).__name__
        manifest["embedding_calls"], manifest["embedding_errors"] = embeddings.calls, embeddings.errors
        manifest["complete"] = bool(manifest.get("measurements_complete") and manifest["cleanup_complete"]
                                    and embeddings.calls == 0 and embeddings.errors == 0)
        save("manifest.json", manifest)
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: isolated owned Docker, cached quality and synthetic scales; exclusive phases, SQL/HTTP counts "
              "and paired instrumentation overhead. No settings/cloud/Agent/real chat/default/optimization changes.")
        return
    if args.cache is None:
        parser.error("--live requires a complete --cache; no implicit paid collection")
    try:
        output, manifest = run_suite(cache=args.cache, repeats=args.repeats, warmups=args.warmups)
    except Exception as exc:  # noqa: BLE001 - safe category only
        parser.exit(1, f"Profile preflight failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not manifest["complete"]:
        parser.exit(1, "Profile incomplete; not scored as memory misses\n")
    print(f"Profiling complete; owned cleanup passed; models 0/0; read path {SNAPSHOT_READ_REVISION}; defaults unchanged")


if __name__ == "__main__":
    main()
