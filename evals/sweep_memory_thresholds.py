"""Replay frozen Kimi memories without Kimi; embed once, sweep many thresholds.

The default command only validates the frozen run and reports missing cached
vectors. ``--embed`` explicitly permits cloud requests for those missing vectors.
Only the tuning split should be used to choose a threshold; run holdout later.
"""

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.chat.memory import (
    _MENTION,
    _SENSITIVE,
    ChatMemory,
    rank_memory_candidates,
)
from app.chat.vector_memory import (
    Embedder,
    EmbeddingEndpoint,
    OpenAICompatibleEmbedder,
    _cosine,
)
from app.config import Settings
from app.team.models import MemberRole
from evals.run_memory_baseline import _normalize_mention
from evals.validate_memory_baseline import (
    CASES_PATH,
    SPLITS_PATH,
    load_cases,
    load_splits,
    validate_cases,
    validate_splits,
)

_REPO = Path(__file__).resolve().parents[1]
CONTROLS_PATH = _REPO / "evals" / "memory-threshold-controls-v1.json"
_RESULTS = _REPO / "evals" / "results" / "memory-threshold-v1"
DEFAULT_THRESHOLDS = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    kind: str
    query: str
    memories: tuple[ChatMemory, ...]
    source_by_id: dict[UUID, str]
    expected: frozenset[str]
    observed_lexical: tuple[str, ...] | None = None
    reason: str | None = None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ratio(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _query_text(case: dict) -> str:
    return _normalize_mention(case["query"]["content"], MemberRole.IMPLEMENTER)


def _make_memory(case_id: str, row: dict, order: int) -> ChatMemory:
    return ChatMemory(
        memory_id=UUID(row["memory_id"]),
        room_id=uuid5(NAMESPACE_URL, f"memory-threshold:{case_id}:room"),
        source_message_id=uuid5(NAMESPACE_URL, f"memory-threshold:{case_id}:{row['source']}"),
        content=row["content"], kind=row["kind"], origin=row["origin"],
        status=row["status"], created_at=f"{order:08d}",
    )


def load_scenarios(
    run_dir: Path, *, controls_path: Path = CONTROLS_PATH,
) -> tuple[str, tuple[Scenario, ...]]:
    """Validate the frozen source and reconstruct production recall boundaries."""
    cases = load_cases()
    validate_cases(cases)
    splits = load_splits()
    validate_splits(cases, splits)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    split = manifest.get("split")
    if split not in {"tuning", "holdout"}:
        raise ValueError("source run must be a frozen tuning or holdout run")
    if manifest.get("agent") != "kimi-code-cli/implementer" or manifest.get("variant") != "current":
        raise ValueError("source run must be the lexical Kimi baseline")
    if manifest.get("dataset_sha256") != hashlib.sha256(CASES_PATH.read_bytes()).hexdigest():
        raise ValueError("source run dataset differs from the frozen cases")
    if manifest.get("splits_sha256") != hashlib.sha256(SPLITS_PATH.read_bytes()).hexdigest():
        raise ValueError("source run split definition differs from the frozen split")
    selected = {case["id"]: case for case in cases if case["id"] in splits[split]}
    originals: dict[str, Scenario] = {}
    for case_id, case in selected.items():
        result = json.loads((run_dir / f"{case_id}.json").read_text(encoding="utf-8"))
        if result.get("case_id") != case_id or not result.get("complete"):
            raise ValueError(f"source case is incomplete or mismatched: {case_id}")
        expected = frozenset(case["gold"]["recall_sources"])
        if set(result["expected_recall_sources"]) != expected:
            raise ValueError(f"source gold differs from frozen dataset: {case_id}")
        messages = {message["id"]: message for message in case["messages"]}
        query = case["query"]
        memories = []
        source_by_id = {}
        for order, row in enumerate(result["memories_before_query"]):
            if row["status"] != "active":
                continue
            source = messages.get(row["source"])
            if source is None:
                raise ValueError(f"memory source is unmapped: {case_id}")
            if (source["room"] != query["room"]
                    or source["discussion"] == query["discussion"]
                    or source.get("external", False) or query.get("external", False)):
                continue
            memory = _make_memory(case_id, row, order)
            memories.append(memory)
            source_by_id[memory.memory_id] = row["source"]
        # Production SQL retrieves newest first, with a 200-active-memory cap.
        memories = list(reversed(memories))[:200]
        scenario = Scenario(
            scenario_id=case_id, kind="frozen_case", query=_query_text(case),
            memories=tuple(memories), source_by_id=source_by_id,
            expected=expected,
            observed_lexical=tuple(result["actual_recall_sources"]),
        )
        replayed = _sources(scenario, rank_memory_candidates(
            scenario.memories, scenario.query,
        ))
        if replayed != scenario.observed_lexical:
            raise ValueError(f"lexical replay differs from the frozen run: {case_id}")
        originals[case_id] = scenario
    controls = json.loads(controls_path.read_text(encoding="utf-8"))
    if controls.get("version") != 1 or not isinstance(controls.get("pairs"), list):
        raise ValueError("invalid counterfactual control file")
    synthetic = []
    seen = set()
    for pair in controls["pairs"]:
        if pair["split"] != split:
            continue
        memory_case, query_case = pair["memory_case"], pair["query_case"]
        if (memory_case not in originals or query_case not in originals
                or memory_case == query_case):
            raise ValueError("counterfactual cases must be distinct and in the same split")
        identity = (memory_case, query_case)
        if identity in seen:
            raise ValueError("duplicate counterfactual control")
        seen.add(identity)
        source = originals[memory_case]
        if not source.memories or not pair.get("reason"):
            raise ValueError("counterfactual must use active memory and explain irrelevance")
        if selected[memory_case]["domain"] != selected[query_case]["domain"]:
            raise ValueError("counterfactual must stay within one domain")
        synthetic.append(Scenario(
            scenario_id=f"{memory_case}__vs__{query_case}",
            kind="counterfactual_irrelevant", query=originals[query_case].query,
            memories=source.memories, source_by_id=source.source_by_id,
            expected=frozenset(), reason=pair["reason"],
        ))
    return split, (*originals.values(), *synthetic)


def _sources(scenario: Scenario, memories: tuple[ChatMemory, ...]) -> tuple[str, ...]:
    return tuple(scenario.source_by_id[memory.memory_id] for memory in memories)


def _semantic_texts(scenarios: tuple[Scenario, ...]) -> tuple[str, ...]:
    texts: dict[str, str] = {}
    for scenario in scenarios:
        if _SENSITIVE.search(scenario.query):
            continue
        safe = [memory for memory in scenario.memories if not _SENSITIVE.search(memory.content)]
        query = _MENTION.sub("", scenario.query).strip()
        if not safe or not query:
            continue
        for value in (query, *(memory.content for memory in safe)):
            texts[_hash(value)] = value
    return tuple(texts.values())


class EmbeddingCache:
    """Model-fingerprinted vector cache; stores text hashes, never credentials."""

    def __init__(self, path: Path, fingerprint: str) -> None:
        self.path = path
        self.fingerprint = fingerprint
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or data.get("model_fingerprint") != fingerprint:
                raise ValueError("embedding cache model/version differs")
            raw = data.get("vectors")
            if not isinstance(raw, dict):
                raise ValueError("invalid embedding cache")
            self.vectors = {key: tuple(value) for key, value in raw.items()}
            for value in self.vectors.values():
                self._validate(value)
            if len({len(value) for value in self.vectors.values()}) > 1:
                raise ValueError("embedding cache mixes dimensions")
        else:
            self.vectors: dict[str, tuple[float, ...]] = {}

    @staticmethod
    def _validate(vector: tuple[float, ...]) -> None:
        if (not 2 <= len(vector) <= 4096
                or not all(isinstance(value, (int, float))
                           and math.isfinite(value) for value in vector)):
            raise ValueError("invalid cached embedding")

    def missing(self, texts: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(text for text in texts if _hash(text) not in self.vectors)

    def ensure(self, texts: tuple[str, ...], embedder: Embedder) -> int:
        missing = self.missing(texts)
        if embedder.model_fingerprint != self.fingerprint:
            raise ValueError("embedder differs from cache identity")
        dimensions = len(next(iter(self.vectors.values()))) if self.vectors else None
        for offset in range(0, len(missing), 10):  # Model Studio max batch size is 10.
            batch = missing[offset:offset + 10]
            vectors = embedder.embed(batch)
            if len(vectors) != len(batch):
                raise ValueError("embedding count differs from input count")
            for text, vector in zip(batch, vectors, strict=True):
                self._validate(vector)
                if dimensions is None:
                    dimensions = len(vector)
                if len(vector) != dimensions:
                    raise ValueError("embedding dimensions changed without revision")
                self.vectors[_hash(text)] = vector
            self.save()  # Preserve successful batches if a later cloud call fails.
        return len(missing)

    def vector(self, text: str) -> tuple[float, ...]:
        return self.vectors[_hash(text)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1, "model_fingerprint": self.fingerprint,
            "vectors": self.vectors,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent,
            prefix=".memory-embeddings-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, self.path)


def _rank_for_threshold(
    scenario: Scenario, cache: EmbeddingCache, threshold: float | None,
) -> tuple[tuple[str, ...], dict[str, float]]:
    if threshold is None or _SENSITIVE.search(scenario.query):
        return _sources(scenario, rank_memory_candidates(
            scenario.memories, scenario.query,
        )), {}
    query = _MENTION.sub("", scenario.query).strip()
    if not query:
        return _sources(scenario, rank_memory_candidates(
            scenario.memories, scenario.query,
        )), {}
    scores = []
    for memory in scenario.memories:
        if _SENSITIVE.search(memory.content):
            continue
        similarity = _cosine(cache.vector(query), cache.vector(memory.content))
        scores.append((memory.memory_id, similarity))
    scores.sort(key=lambda item: (-item[1], str(item[0])))
    semantic = tuple((memory_id, score) for memory_id, score in scores
                     if score >= threshold)
    retrieved = rank_memory_candidates(scenario.memories, scenario.query, semantic)
    return _sources(scenario, retrieved), {
        scenario.source_by_id[memory_id]: round(score, 4) for memory_id, score in scores
    }


def _counts(scenarios: tuple[Scenario, ...], predictions: dict[str, tuple[str, ...]]) -> dict:
    tp = fp = fn = 0
    negatives_hit = 0
    for scenario in scenarios:
        actual = set(predictions[scenario.scenario_id])
        tp += len(actual & scenario.expected)
        fp += len(actual - scenario.expected)
        fn += len(scenario.expected - actual)
        if not scenario.expected and actual:
            negatives_hit += 1
    return {
        "scenario_count": len(scenarios), "tp": tp, "fp": fp, "fn": fn,
        "recall_at_3": _ratio(tp, tp + fn),
        "precision_at_3": _ratio(tp, tp + fp),
        "negative_scenarios_with_recall": negatives_hit,
    }


def sweep(
    scenarios: tuple[Scenario, ...], cache: EmbeddingCache,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
) -> dict:
    if not thresholds or any(not -1 <= value <= 1 for value in thresholds):
        raise ValueError("thresholds must be nonempty and between -1 and 1")
    missing = cache.missing(_semantic_texts(scenarios))
    if missing:
        raise ValueError(f"{len(missing)} embeddings are missing from cache")
    original = tuple(item for item in scenarios if item.kind == "frozen_case")
    controls = tuple(item for item in scenarios if item.kind == "counterfactual_irrelevant")
    rows = []
    for threshold in (None, *thresholds):
        predictions = {}
        similarities = {}
        for scenario in scenarios:
            recalled, scores = _rank_for_threshold(scenario, cache, threshold)
            predictions[scenario.scenario_id] = recalled
            similarities[scenario.scenario_id] = scores
        rows.append({
            "mode": "lexical" if threshold is None else "hybrid",
            "threshold": threshold,
            "frozen_cases": _counts(original, predictions),
            "counterfactual_irrelevant": _counts(controls, predictions),
            "scenarios": [{
                "id": scenario.scenario_id, "kind": scenario.kind,
                "expected_sources": sorted(scenario.expected),
                "recalled_sources": list(predictions[scenario.scenario_id]),
                "similarities_by_source": similarities[scenario.scenario_id],
                "reason": scenario.reason,
            } for scenario in scenarios],
        })
    return {"model_fingerprint": cache.fingerprint, "thresholds": rows}


def _parse_thresholds(raw: str) -> tuple[float, ...]:
    try:
        values = tuple(float(part.strip()) for part in raw.split(","))
    except ValueError as exc:
        raise ValueError("thresholds must be comma-separated numbers") from exc
    if not values or any(not math.isfinite(value) or not -1 <= value <= 1
                         for value in values) or len(set(values)) != len(values):
        raise ValueError("thresholds must be distinct finite numbers between -1 and 1")
    return values


def load_candidate(
    path: Path, endpoint: EmbeddingEndpoint, *, report_root: Path = _RESULTS,
) -> float:
    """Require a tuning decision frozen before touching holdout vectors."""
    candidate = json.loads(path.read_text(encoding="utf-8"))
    threshold = candidate.get("threshold")
    if (candidate.get("version") != 1
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not -1 <= threshold <= 1
            or candidate.get("status") != "provisional_holdout_candidate"):
        raise ValueError("invalid locked threshold candidate")
    if (candidate.get("model_fingerprint") != endpoint.model_fingerprint
            or candidate.get("model") != endpoint.model
            or candidate.get("revision") != endpoint.revision):
        raise ValueError("candidate embedding model differs from current configuration")
    if (candidate.get("dataset_sha256")
            != hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
            or candidate.get("controls_sha256")
            != hashlib.sha256(CONTROLS_PATH.read_bytes()).hexdigest()):
        raise ValueError("candidate dataset or controls changed after tuning")
    report_name = candidate.get("tuning_report")
    if not isinstance(report_name, str) or Path(report_name).name != report_name:
        raise ValueError("invalid candidate tuning report name")
    source = report_root / report_name
    if hashlib.sha256(source.read_bytes()).hexdigest() != candidate.get("tuning_report_sha256"):
        raise ValueError("candidate tuning report differs from frozen evidence")
    tuning = json.loads(source.read_text(encoding="utf-8"))
    if (tuning.get("split") != "tuning"
            or Path(tuning.get("source_run", "")).name != candidate.get("tuning_run")
            or not any(row["threshold"] == threshold
                       for row in tuning.get("thresholds", []))):
        raise ValueError("candidate does not match its tuning report")
    return float(threshold)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True,
                        help="frozen Kimi tuning or holdout result directory")
    parser.add_argument("--embed", action="store_true",
                        help="call the configured cloud API for missing vectors")
    parser.add_argument("--thresholds", help="comma-separated tuning thresholds")
    parser.add_argument("--candidate", type=Path,
                        help="locked threshold decision, required for holdout")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output-root", type=Path, default=_RESULTS)
    args = parser.parse_args()
    split, scenarios = load_scenarios(args.run.resolve())
    settings = Settings()
    if args.embed and settings.memory_embedding_mode != "on":
        parser.error("--embed requires CODECREW_MEMORY_EMBEDDING_MODE=on")
    endpoint = EmbeddingEndpoint(
        base_url=settings.memory_embedding_base_url,
        model=settings.memory_embedding_model,
        revision=settings.memory_embedding_revision,
        api_key=(settings.memory_embedding_api_key.get_secret_value()
                 if args.embed else "cache-only"),
        timeout_seconds=settings.memory_embedding_timeout_seconds,
    )
    if split == "holdout":
        if args.candidate is None or args.thresholds is not None:
            parser.error("holdout requires --candidate and forbids --thresholds")
        thresholds = (load_candidate(args.candidate.resolve(), endpoint),)
    else:
        if args.candidate is not None:
            parser.error("--candidate is only for the holdout split")
        thresholds = _parse_thresholds(
            args.thresholds or ",".join(map(str, DEFAULT_THRESHOLDS)),
        )
    cache_path = (args.cache.resolve() if args.cache else
                  args.output_root.resolve() / f"cache-{endpoint.model_fingerprint}.json")
    cache = EmbeddingCache(cache_path, endpoint.model_fingerprint)
    texts = _semantic_texts(scenarios)
    missing = cache.missing(texts)
    if missing and not args.embed:
        print(f"Validated {len(scenarios)} {split} scenarios; {len(missing)} unique texts need "
              "embeddings. No model called; rerun with --embed.")
        return
    if missing:
        embedded = cache.ensure(texts, OpenAICompatibleEmbedder(endpoint))
        print(f"Embedded {embedded} unique synthetic texts; cache: {cache_path}")
    report = sweep(scenarios, cache, thresholds)
    report.update({
        "split": split, "source_run": str(args.run.resolve()),
        "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "controls_sha256": hashlib.sha256(CONTROLS_PATH.read_bytes()).hexdigest(),
        "embedding": {"model": endpoint.model, "revision": endpoint.revision,
                      "fingerprint": endpoint.model_fingerprint},
        "cache": str(cache_path),
        "locked_candidate": (str(args.candidate.resolve())
                             if args.candidate is not None else None),
        "limitations": [
            "Only frozen Kimi memory writes are replayed; Agent answers are not regenerated.",
            "Counterfactual irrelevant pairs are synthetic and need human semantic review.",
            "The source runs are pilot data, not a blind holdout or causal A/B test.",
        ],
    })
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = args.output_root.resolve() / f"{run_id}-{split}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in report["thresholds"]:
        frozen = row["frozen_cases"]
        controls = row["counterfactual_irrelevant"]
        label = "lexical" if row["threshold"] is None else f"{row['threshold']:.2f}"
        print(f"{label:>7}: recall {frozen['tp']}/{frozen['tp'] + frozen['fn']}, "
              f"frozen FP {frozen['fp']}, irrelevant-control FP {controls['fp']}")
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
