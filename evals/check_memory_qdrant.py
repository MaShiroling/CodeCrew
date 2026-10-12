"""Opt-in real Qdrant storage probe. Synthetic vectors, zero embedding/Agent.

Default prints a plan without settings/network. --live creates one uniquely
named disposable collection, then deletes only that collection in finally.
It never opens a canonical chat database or measures model recall quality.
"""

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.chat.qdrant_vector_backend import QdrantEndpoint, QdrantMemoryVectorBackend
from app.chat.vector_contracts import VectorAnchor, VectorModel, VectorRecord, VectorSearch
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals/results/memory-qdrant-probe-v1"


def probe(*, output_root=RESULTS):
    settings = Settings()
    prefix = "codecrew_probe_" + uuid4().hex
    endpoint = QdrantEndpoint(
        base_url=settings.memory_qdrant_url, collection_prefix=prefix,
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds,
    )
    model = VectorModel("codecrew-storage-probe-v1", 3)
    backend = QdrantMemoryVectorBackend(endpoint, model)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = output_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    report = {"complete": False, "collection": backend.collection_name,
              "checks": [], "cleanup_complete": False, "embedding_calls": 0,
              "agent_calls": 0, "vectors": "synthetic storage plumbing only",
              "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                              for path in ("app/chat/qdrant_vector_backend.py", "app/chat/vector_contracts.py",
                                           "evals/check_memory_qdrant.py")}}
    attempted = False
    try:
        # The prefix is freshly generated and never loaded from private settings.
        # GET first; only initialize a missing collection, never adopt existing data.
        if backend._collection() is not None:
            raise RuntimeError("probe collection collision")
        attempted = True
        backend.initialize()
        room, other_room = uuid4(), uuid4()
        def item(room_id, memory_id=None):
            return VectorAnchor(memory_id or uuid4(), room_id, uuid4(), uuid4(), "a" * 64)
        a, b = item(room), item(room)
        # Same logical ID in another room is an intentional isolation check.
        foreign = item(other_room, a.memory_id)
        records = (VectorRecord(a, model, (1.0, 0.0, 0.0)),
                   VectorRecord(b, model, (0.8, 0.6, 0.0)),
                   VectorRecord(foreign, model, (1.0, 0.0, 0.0)))
        if backend.upsert(records) != (a.memory_id, b.memory_id, foreign.memory_id):
            raise RuntimeError("probe upsert failed")
        backend.upsert(records)
        report["checks"].append("collection_indexes_and_idempotent_upsert")
        request = VectorSearch(room, model, (1.0, 0.0, 0.0), (a, b))
        hits = backend.search(request)
        if [hit.anchor for hit in hits] != [a, b] or abs(hits[1].similarity - 0.8) > 1e-5:
            raise RuntimeError("probe exact cosine failed")
        report["checks"].append("candidate_scoped_exact_cosine")
        stale = replace(a, content_hash="b" * 64)
        stale_hits = backend.search(replace(request, candidates=(stale, b), limit=1))
        if [hit.anchor for hit in stale_hits] != [b]:
            raise RuntimeError("probe stale filtering before limit failed")
        report["checks"].append("stale_anchor_filtered_before_limit")
        backend.delete(room, (a.memory_id,))
        backend.delete(room, (a.memory_id,))
        if [hit.anchor for hit in backend.search(request)] != [b]:
            raise RuntimeError("probe delete failed")
        foreign_request = VectorSearch(other_room, model, (1.0, 0.0, 0.0), (foreign,))
        if [hit.anchor for hit in backend.search(foreign_request)] != [foreign]:
            raise RuntimeError("probe room isolation failed")
        report["checks"].append("room_scoped_idempotent_delete")
        report["storage_checks_passed"] = True
    except Exception as exc:  # noqa: BLE001 - preserve safe failure evidence, no provider bodies
        report["error_type"] = type(exc).__name__
    finally:
        if attempted:
            try:
                if backend._request("DELETE", backend._path) is not True:
                    raise RuntimeError("probe cleanup failed")
                if backend._collection() is not None:
                    raise RuntimeError("probe collection still exists")
                report["cleanup_complete"] = True
            except Exception as exc:  # noqa: BLE001 - report safe category only
                report["cleanup_error_type"] = type(exc).__name__
        report["complete"] = bool(report.get("storage_checks_passed") and report["cleanup_complete"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicit local service writes and owned cleanup")
    args = parser.parse_args()
    if not args.live:
        print("Plan only: --live checks Qdrant storage with synthetic vectors, zero embedding/Agent; "
              "creates and removes only a uniquely named probe collection. No settings/network opened.")
        return
    try:
        output, report = probe()
    except Exception as exc:  # noqa: BLE001 - never print settings/provider response
        parser.exit(1, f"Probe failed: {type(exc).__name__}\n")
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Probe incomplete; inspect safe report, not a completed service acceptance\n")
    print("Real Qdrant storage checks and owned collection cleanup passed; embedding/Agent calls 0/0")


if __name__ == "__main__":
    main()
