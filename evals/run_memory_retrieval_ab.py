"""Paired lexical/hybrid retrieval comparison with identical seeded memories.

Default mode only validates the frozen candidate dataset. ``--embed`` is
explicitly billable and requires an independent label review in the lock file.
No Agent CLI is invoked: this isolates retrieval from stochastic memory writing.
"""

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.chat.models import ExternalChatSource, StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import (
    Embedder,
    EmbeddingEndpoint,
    MemoryVectorIndex,
    OpenAICompatibleEmbedder,
)
from app.config import Settings
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from evals.validate_memory_unseen import LOCK_PATH, validate_unseen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results" / "memory-retrieval-ab-v1"


class CountingEmbedder:
    """Record request volume without logging texts, vectors, or credentials."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.model_fingerprint = inner.model_fingerprint
        self.calls = 0
        self.texts = 0
        self.errors = 0

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        self.texts += len(texts)
        try:
            return self.inner.embed(texts)
        except Exception:
            self.errors += 1
            raise


def _id(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "codecrew:memory-retrieval-ab-v1:" + ":".join(parts))


def _seed_case(case: dict, path: Path) -> tuple[StandaloneChatStore, UUID, UUID, dict[UUID, str]]:
    """Inject labeled memory text, never Agent-generated text, into real SQLite chat."""
    store = StandaloneChatStore(SQLiteDatabase(path))
    store.initialize()
    service = StandaloneChatService(store)
    rooms = {}
    messages = {}
    for item in case["messages"]:
        symbol = item["room"]
        if symbol not in rooms:
            rooms[symbol] = service.create_room(
                title=f"Retrieval A/B {case['id']} / {symbol}",
                idempotency_key=_id(case["id"], "room", symbol),
            )
        room = rooms[symbol]
        human = next(member for member in room.members if member.role is MemberRole.HUMAN)
        agent = next(member for member in room.members if member.role is MemberRole.PLANNER)
        is_human = item["role"] == "human"
        message_id = _id(case["id"], "message", item["id"])
        store.append_message(StandaloneChatMessage(
            message_id=message_id, room_id=room.room_id, trace_id=room.trace_id,
            sender_id=human.member_id if is_human else agent.member_id,
            recipient_ids=(agent.member_id if is_human else human.member_id,),
            content=item["content"],
            correlation_id=_id(case["id"], symbol, item["discussion"]),
            external_source=(ExternalChatSource(
                external_chat_id="memory-ab", external_sender_id="fixture",
            ) if item.get("external", False) else None),
            idempotency_key=f"memory-ab:{case['id']}:{item['id']}",
        ))
        messages[item["id"]] = (room.room_id, message_id)

    by_source = {}
    for write in case["gold"]["writes"]:
        room_id, source_message_id = messages[write["source"]]
        replaced = by_source.get(write.get("replaces"))
        memory = service.create_memory(
            room_id, source_message_id=source_message_id, content=write["fact"],
            replaces_memory_id=replaced.memory_id if replaced else None,
        )
        by_source[write["source"]] = memory

    query = case["query"]
    if query["room"] not in rooms:
        rooms[query["room"]] = service.create_room(
            title=f"Retrieval A/B {case['id']} / {query['room']}",
            idempotency_key=_id(case["id"], "room", query["room"]),
        )
    query_room = rooms[query["room"]].room_id
    query_discussion = _id(case["id"], query["room"], query["discussion"])
    source_by_id = {memory.source_message_id: source for source, memory in by_source.items()}
    return store, query_room, query_discussion, source_by_id


def _scores(expected: set[str], actual: list[str]) -> dict:
    actual_set = set(actual)
    return {
        "tp": len(expected & actual_set),
        "fp": len(actual_set - expected),
        "fn": len(expected - actual_set),
        "recall_at_3": (len(expected & actual_set) / len(expected) if expected else None),
        "false_positive_on_empty_gold": not expected and bool(actual_set),
    }


def compare_case(case: dict, embedder: CountingEmbedder, *, min_similarity: float) -> dict:
    """Run both production retrieval paths on one unmodified memory snapshot."""
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-ab-") as scratch:
        store, room_id, discussion_id, source_by_id = _seed_case(
            case, Path(scratch) / "chat.sqlite3",
        )
        before = tuple((memory.memory_id, memory.content, memory.status)
                       for memory in store.memory.list_active(room_id))
        lexical = store.memory.retrieve(
            room_id, case["query"]["content"], exclude_correlation_id=discussion_id,
        )
        index = MemoryVectorIndex(
            store.database, embedder, min_similarity=min_similarity,
        )
        store.memory.vector_index = index
        # Reproduce production's one-memory-at-a-time indexing and sensitive
        # content guard, rather than assuming a cheaper evaluation-only batch.
        for memory in store.memory.list_active(room_id):
            store.memory._index_if_enabled(memory)
        hybrid = store.memory.retrieve(
            room_id, case["query"]["content"], exclude_correlation_id=discussion_id,
        )
        after = tuple((memory.memory_id, memory.content, memory.status)
                      for memory in store.memory.list_active(room_id))
        if before != after or embedder.errors:
            raise RuntimeError("paired retrieval comparison did not complete cleanly")
        lexical_sources = [source_by_id[memory.source_message_id] for memory in lexical]
        hybrid_sources = [source_by_id[memory.source_message_id] for memory in hybrid]
        expected = set(case["gold"]["recall_sources"])
        return {
            "case_id": case["id"], "domain": case["domain"],
            "expected_sources": sorted(expected),
            "active_sources": case["gold"]["active_sources"],
            "lexical": {"sources": lexical_sources, **_scores(expected, lexical_sources)},
            "hybrid": {"sources": hybrid_sources, **_scores(expected, hybrid_sources)},
            "same_seeded_memory_snapshot": True,
        }


def summarize(results: list[dict]) -> dict:
    report = {"cases": len(results)}
    for variant in ("lexical", "hybrid"):
        counts = Counter({name: sum(item[variant][name] for item in results)
                          for name in ("tp", "fp", "fn")})
        report[variant] = {
            **counts,
            "recall_at_3": (counts["tp"] / (counts["tp"] + counts["fn"])
                            if counts["tp"] + counts["fn"] else None),
            "precision_at_3": (counts["tp"] / (counts["tp"] + counts["fp"])
                               if counts["tp"] + counts["fp"] else None),
            "false_positive_queries_with_empty_gold": sum(
                item[variant]["false_positive_on_empty_gold"] for item in results
            ),
        }
    report["paired_wins"] = [item["case_id"] for item in results
                             if item["hybrid"]["tp"] > item["lexical"]["tp"]
                             and item["hybrid"]["fp"] <= item["lexical"]["fp"]]
    report["paired_losses"] = [item["case_id"] for item in results
                               if item["hybrid"]["tp"] < item["lexical"]["tp"]
                               or item["hybrid"]["fp"] > item["lexical"]["fp"]]
    return report


def _require_review(lock: dict) -> None:
    if (lock.get("label_provenance") != "independently_human_reviewed"
            or not lock.get("reviewer") or not lock.get("reviewed_at")):
        raise ValueError("independent human label review is required before embedding")


def run_suite(*, output_root: Path = RESULTS) -> Path:
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
    embedder = CountingEmbedder(OpenAICompatibleEmbedder(endpoint))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    result_dir = output_root / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = compare_case(
            case, embedder, min_similarity=settings.memory_embedding_min_similarity,
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
                         "evals/run_memory_retrieval_ab.py")
        },
        "embedding": {"model": endpoint.model, "revision": endpoint.revision,
                      "fingerprint": endpoint.model_fingerprint,
                      "min_similarity": settings.memory_embedding_min_similarity},
        "agent_calls": 0,
        "embedding_calls": embedder.calls,
        "embedding_texts": embedder.texts,
        "billing_cost": None,
        "label_review": {"reviewer": lock["reviewer"],
                         "reviewed_at": lock["reviewed_at"]},
    }
    (result_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (result_dir / "summary.json").write_text(
        json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embed", action="store_true",
                        help="after independent review, call the configured cloud embedding API")
    args = parser.parse_args()
    if args.embed:
        print(f"Results: {run_suite()}")
    else:
        cases = validate_unseen()
        print(f"Validated {len(cases)} frozen cases; no Agent or embedding API calls made")


if __name__ == "__main__":
    main()
