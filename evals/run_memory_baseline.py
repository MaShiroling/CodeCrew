"""Replay memory cases through the real chat dispatcher, one room database per case.

The default command only validates its inputs. ``--live`` explicitly invokes the
selected Agent CLI and may consume model quota. ``--memory-mode hybrid`` also
calls the configured cloud embedding API. Results are written under the ignored
evals/results directory; no coding task or repository workspace is used.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.agents import AgentAdapter, CodexCliAdapter, KimiCodeAdapter
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.dispatch import StandaloneChatDispatcher, chat_context
from app.chat.memory import AgentMemoryProposal, ChatMemory, ChatMemoryStore
from app.chat.models import ExternalChatSource, StandaloneChatMessage, StoredStandaloneChatMessage
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
from app.workspace.permissions import PermissionPolicy
from evals.validate_memory_baseline import (
    CASES_PATH,
    SPLITS_PATH,
    load_cases,
    load_splits,
    validate_cases,
    validate_splits,
)

_REPO = Path(__file__).resolve().parents[1]
_RESULTS = _REPO / "evals" / "results" / "memory-baseline-v1"
_MENTION = re.compile(r"@(白金|月见|鲸鲸)")


class _NoMemoryStore(ChatMemoryStore):
    """Evaluation-only control: same chat execution without memory writes/reads."""

    def capture_agent_proposal(
        self, room_id: UUID, source_message_id: UUID, proposal: AgentMemoryProposal,
    ) -> None:
        return None

    def retrieve(
        self, room_id: UUID, query: str, *, limit: int = 3,
        exclude_correlation_id: UUID | None = None,
    ) -> tuple[ChatMemory, ...]:
        return ()


class _SingleAgentEvalDispatcher(StandaloneChatDispatcher):
    """Keep production chat logic while making the one-Agent eval explicit."""

    def _prompt(self, role, stored, room) -> str:
        return (super()._prompt(role, stored, room)
                + "\n评测约束：本轮只由你一位 Agent 作答。请完整回答，handoff_to 必须为空数组。")


def _identity(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "codecrew:memory-baseline-v1:" + ":".join(parts))


def _normalize_mention(content: str, role: MemberRole) -> str:
    """Route every synthetic turn to the selected single Agent."""
    return _MENTION.sub("@月见" if role is MemberRole.IMPLEMENTER else "@白金", content)


def _member(room, role: MemberRole) -> UUID:
    return next(member.member_id for member in room.members if member.role is role)


def _result_memory_rows(store: StandaloneChatStore, source_ids: dict[str, str]) -> list[dict]:
    with store.database.connect() as connection:
        rows = connection.execute(
            "SELECT memory_id, source_message_id, content, kind, origin, status, "
            "replaces_memory_id FROM standalone_chat_memories ORDER BY created_at, memory_id"
        ).fetchall()
    by_memory_id = {row["memory_id"]: row for row in rows}
    result = []
    for row in rows:
        old = by_memory_id.get(row["replaces_memory_id"])
        result.append({
            "memory_id": row["memory_id"],
            "source": source_ids.get(row["source_message_id"], "unmapped"),
            "content": row["content"], "kind": row["kind"],
            "origin": row["origin"], "status": row["status"],
            "replaces_source": (source_ids.get(old["source_message_id"])
                                if old is not None else None),
        })
    return result


def _turn_result(
    store: StandaloneChatStore, stored: StoredStandaloneChatMessage,
) -> dict:
    turns = [turn for turn in store.list_turns(stored.message.room_id)
             if turn.message_id == stored.message.message_id]
    replies = [item for item in store.list_messages(
        stored.message.room_id, after_sequence=stored.sequence, limit=100,
    ) if item.message.reply_to == stored.message.message_id
        and item.message.sender_id != stored.message.sender_id]
    return {
        "status": turns[0].status.value if turns else "not_dispatched",
        "error": turns[0].error if turns else None,
        "reply": replies[0].message.content if replies else None,
    }


def _source_metrics(expected: set[str], actual: set[str]) -> dict[str, int]:
    return {
        "tp": len(expected & actual),
        "fp": len(actual - expected),
        "fn": len(expected - actual),
    }


async def run_case(
    case: dict, adapter: AgentAdapter | None, *, variant: str = "current",
    agent_role: MemberRole = MemberRole.PLANNER,
    timeout_seconds: int = 180,
    embedder: Embedder | None = None,
    min_similarity: float = 0.65,
) -> dict:
    """Run one synthetic case with production persistence and dispatch logic."""
    if variant not in {"current", "no-memory", "hybrid"}:
        raise ValueError("unknown memory evaluation variant")
    if variant == "hybrid" and embedder is None:
        raise ValueError("hybrid evaluation requires an embedding provider")
    if agent_role not in {MemberRole.PLANNER, MemberRole.IMPLEMENTER}:
        raise ValueError("memory evaluation requires planner or implementer")
    temp_root = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="codecrew-memory-eval-", dir=temp_root) as scratch:
        root = Path(scratch).resolve()
        database = SQLiteDatabase(root / "chat.sqlite3")
        vector_index = (MemoryVectorIndex(
            database, embedder, min_similarity=min_similarity,
        ) if variant == "hybrid" and embedder is not None else None)
        store = StandaloneChatStore(database, vector_index=vector_index)
        store.initialize()
        if variant == "no-memory":
            store.memory = _NoMemoryStore(store.database)
        service = StandaloneChatService(store)
        workspaces = StandaloneChatWorkspaceManager(root / "workspaces", root / "runtime")
        if adapter is None:
            if agent_role is not MemberRole.IMPLEMENTER:
                raise ValueError("automatic adapter construction is only for Kimi")
            settings = Settings()
            adapter = KimiCodeAdapter(
                worktree_root=workspaces.workspace_root,
                runtime_root=workspaces.runtime_root,
                policy=PermissionPolicy(allowed_paths=(".",)),
                executable=settings.kimi_cli_path,
                allow_standalone_chat=True,
            )
        runtime = StandaloneChatAgentRuntime(
            workspaces,
            {role: adapter for role in (
                MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER,
            )},
        )
        dispatcher = _SingleAgentEvalDispatcher(
            store, runtime, max_turns_per_thread=6, timeout_seconds=timeout_seconds,
        )
        await dispatcher.startup()
        rooms = {}
        source_ids: dict[str, str] = {}
        setup_turns = []

        def room_for(symbol: str):
            if symbol not in rooms:
                rooms[symbol] = service.create_room(
                    title=f"Memory eval {case['id']} / {symbol}",
                    idempotency_key=_identity(case["id"], "room", symbol),
                )
            return rooms[symbol]

        def append(symbolic_id: str, item: dict) -> StoredStandaloneChatMessage:
            room = room_for(item["room"])
            is_human = item.get("role", "human") == "human"
            human_id = _member(room, MemberRole.HUMAN)
            agent_id = _member(room, agent_role)
            external = (ExternalChatSource(
                external_chat_id="memory-eval", external_sender_id="fixture",
            ) if item.get("external", False) else None)
            stored = store.append_message(StandaloneChatMessage(
                message_id=_identity(case["id"], "message", symbolic_id),
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=human_id if is_human else agent_id,
                recipient_ids=(agent_id if is_human else human_id,),
                content=(_normalize_mention(item["content"], agent_role) if is_human
                         else item["content"]),
                external_source=external,
                correlation_id=_identity(case["id"], item["room"], item["discussion"]),
                idempotency_key=f"memory-eval:{case['id']}:{symbolic_id}",
            ))
            source_ids[str(stored.message.message_id)] = symbolic_id
            return stored

        try:
            for item in case["messages"]:
                stored = append(item["id"], item)
                if item["role"] == "human":
                    dispatcher.enqueue(stored)
                    await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
                    setup_turns.append({"source": item["id"], **_turn_result(store, stored)})

            # Snapshot writes before the query, so the query itself is never graded as a write.
            memories = _result_memory_rows(store, source_ids)
            actual_writes = {memory["source"] for memory in memories
                             if memory["origin"] == "agent_auto"}
            active = {memory["source"] for memory in memories
                      if memory["status"] == "active"}
            query = append("query", {**case["query"], "role": "human"})
            context = json.loads(await asyncio.to_thread(
                chat_context, store, query, room_for(case["query"]["room"]),
            ))
            recalled = [source_ids.get(item["source_message_id"], "unmapped")
                        for item in context["room_memories"]]
            dispatcher.enqueue(query)
            await asyncio.wait_for(dispatcher.wait_idle(), timeout_seconds + 30)
            query_turn = _turn_result(store, query)
            answer = query_turn["reply"] or ""
            expected = case["gold"]
            all_turns = [turn for room in rooms.values()
                         for turn in store.list_turns(room.room_id)]
            room_by_id = {room.room_id: room for room in rooms.values()}
            only_selected_agent = all(
                turn.recipient_id == _member(room_by_id[turn.room_id], agent_role)
                for turn in all_turns
            )
            complete = (all(turn["status"] == "succeeded" for turn in setup_turns)
                        and query_turn["status"] == "succeeded"
                        and len(all_turns) == len(setup_turns) + 1 and only_selected_agent)
            return {
                "case_id": case["id"], "domain": case["domain"],
                "tags": case["tags"], "variant": variant, "complete": complete,
                "agent_role": agent_role.value,
                "setup_turns": setup_turns, "query_turn": query_turn,
                "total_agent_turns": len(all_turns),
                "memories_before_query": memories,
                "expected_write_sources": [write["source"] for write in expected["writes"]],
                "actual_write_sources": sorted(actual_writes),
                "expected_active_sources": expected["active_sources"],
                "actual_active_sources": sorted(active),
                "expected_recall_sources": expected["recall_sources"],
                "actual_recall_sources": recalled,
                "answer": answer,
                "answer_review": {
                    "status": "pending_human_review",
                    "must_include": expected["answer_must_include"],
                    "must_not_include": expected["answer_must_not_include"],
                },
                "write_source_counts": _source_metrics(
                    {write["source"] for write in expected["writes"]}, actual_writes,
                ),
                "recall_source_counts": _source_metrics(
                    set(expected["recall_sources"]), set(recalled),
                ),
            }
        finally:
            await dispatcher.shutdown()


def summarize(results: list[dict]) -> dict:
    complete = [item for item in results if item.get("complete")]
    counts = {
        group: {key: sum(item[f"{group}_source_counts"][key] for item in complete)
                for key in ("tp", "fp", "fn")}
        for group in ("write", "recall")
    }
    return {
        "case_count": len(results), "complete_count": len(complete),
        "incomplete_cases": [item["case_id"] for item in results if not item.get("complete")],
        "source_counts": counts,
        "write_precision": _ratio(counts["write"]["tp"],
                                  counts["write"]["tp"] + counts["write"]["fp"]),
        "write_recall": _ratio(counts["write"]["tp"],
                               counts["write"]["tp"] + counts["write"]["fn"]),
        "recall_at_3": _ratio(counts["recall"]["tp"],
                              counts["recall"]["tp"] + counts["recall"]["fn"]),
        "answer_quality": "pending_human_review",
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _fingerprint() -> dict:
    tracked = (
        "app/chat/memory.py", "app/chat/vector_memory.py",
        "app/chat/dispatch.py", "app/chat/store.py",
        "evals/memory-baseline-v1.jsonl", "evals/run_memory_baseline.py",
    )
    hashes = {name: hashlib.sha256((_REPO / name).read_bytes()).hexdigest() for name in tracked}
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_REPO, capture_output=True, text=True, check=False,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=_REPO, capture_output=True, text=True,
        check=False,
    )
    return {"commit": commit.stdout.strip() or None, "dirty": bool(dirty.stdout),
            "sha256": hashes}


def _completed_prefix(
    cases: list[dict], resume_from: Path, fingerprint: dict, agent: str, split: str,
    variant: str, embedding_fingerprint: str | None, min_similarity: float | None,
) -> list[dict]:
    """Reuse only consecutive, complete cases from a compatible earlier run."""
    manifest = json.loads((resume_from / "manifest.json").read_text(encoding="utf-8"))
    expected_agent = ("kimi-code-cli/implementer" if agent == "kimi"
                      else "codex-cli/planner")
    if manifest["agent"] != expected_agent:
        raise ValueError("resume agent differs from the earlier run")
    if manifest.get("split", "all") != split:
        raise ValueError("resume split differs from the earlier run")
    if manifest.get("variant") != variant:
        raise ValueError("resume memory variant differs from the earlier run")
    old_embedding = manifest.get("embedding") or {}
    if old_embedding.get("fingerprint") != embedding_fingerprint:
        raise ValueError("resume embedding model differs from the earlier run")
    if old_embedding.get("min_similarity") != min_similarity:
        raise ValueError("resume embedding threshold differs from the earlier run")
    if manifest["dataset_sha256"] != hashlib.sha256(CASES_PATH.read_bytes()).hexdigest():
        raise ValueError("resume dataset differs from the earlier run")
    for name, digest in fingerprint["sha256"].items():
        if name == "evals/run_memory_baseline.py":
            continue  # Adding resume support does not alter chat behavior.
        if manifest["code"]["sha256"].get(name) != digest:
            raise ValueError(f"resume code differs from the earlier run: {name}")
    results = []
    for case in cases:
        path = resume_from / f"{case['id']}.json"
        if not path.exists():
            break
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["case_id"] != case["id"] or not result.get("complete"):
            break
        results.append(result)
    return results


async def run_live(
    *, case_id: str | None, output_root: Path, timeout_seconds: int,
    resume_from: Path | None = None, agent: str = "codex", split: str = "all",
    memory_mode: str = "lexical",
) -> Path:
    cases = load_cases()
    validate_cases(cases)
    splits = load_splits()
    validate_splits(cases, splits)
    if split not in {"all", "tuning", "holdout"}:
        raise ValueError("unknown evaluation split")
    if split != "all":
        selected = set(splits[split])
        cases = [case for case in cases if case["id"] in selected]
    if case_id is not None:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            raise ValueError(f"unknown case: {case_id}")
    if resume_from is not None and case_id is not None:
        raise ValueError("--resume-from cannot be combined with --case")
    fingerprint = _fingerprint()
    if agent not in {"codex", "kimi"}:
        raise ValueError("unknown evaluation agent")
    if memory_mode not in {"lexical", "hybrid"}:
        raise ValueError("unknown evaluation memory mode")
    settings = Settings()
    variant = "hybrid" if memory_mode == "hybrid" else "current"
    endpoint = None
    embedder = None
    if variant == "hybrid":
        if settings.memory_embedding_mode != "on":
            raise ValueError("hybrid evaluation requires CODECREW_MEMORY_EMBEDDING_MODE=on")
        endpoint = EmbeddingEndpoint(
            base_url=settings.memory_embedding_base_url,
            model=settings.memory_embedding_model,
            revision=settings.memory_embedding_revision,
            api_key=settings.memory_embedding_api_key.get_secret_value(),
            timeout_seconds=settings.memory_embedding_timeout_seconds,
        )
        embedder = OpenAICompatibleEmbedder(endpoint)
    completed = (_completed_prefix(
        cases, resume_from, fingerprint, agent, split, variant,
        endpoint.model_fingerprint if endpoint else None,
        settings.memory_embedding_min_similarity if endpoint else None,
    ) if resume_from else [])
    executable = settings.kimi_cli_path if agent == "kimi" else settings.codex_cli_path
    if shutil.which(executable) is None:
        raise RuntimeError(f"{agent} CLI unavailable: {executable}")
    if agent == "kimi" and not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        raise RuntimeError("KIMI_MODEL_API_KEY is required for Kimi evaluation")
    adapter = CodexCliAdapter(executable=executable) if agent == "codex" else None
    agent_role = MemberRole.PLANNER if agent == "codex" else MemberRole.IMPLEMENTER
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id, "variant": variant,
        "agent": ("codex-cli/planner" if agent == "codex"
                  else "kimi-code-cli/implementer"),
        "model_note": ("Codex CLI default model; not pinned by CodeCrew adapter"
                       if agent == "codex" else "Kimi Code adapter alias: kimi-for-coding"),
        "prompt_note": "Production chat prompt plus single-Agent handoff_to=[] instruction",
        "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "split": split,
        "splits_sha256": hashlib.sha256(SPLITS_PATH.read_bytes()).hexdigest(),
        "code": fingerprint, "timeout_seconds": timeout_seconds,
        "embedding": ({
            "model": endpoint.model,
            "revision": endpoint.revision,
            "fingerprint": endpoint.model_fingerprint,
            "min_similarity": settings.memory_embedding_min_similarity,
        } if endpoint else None),
        "resume_from": str(resume_from) if resume_from else None,
        "reused_complete_cases": [item["case_id"] for item in completed],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    results = list(completed)
    for result in completed:
        source = resume_from / f"{result['case_id']}.json"
        shutil.copy2(source, run_dir / source.name)
    if results:
        (run_dir / "summary.json").write_text(
            json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8",
        )
    for index, case in enumerate(cases[len(completed):], len(completed) + 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = await run_case(
            case, adapter, variant=variant, agent_role=agent_role,
            timeout_seconds=timeout_seconds, embedder=embedder,
            min_similarity=settings.memory_embedding_min_similarity,
        )
        results.append(result)
        (run_dir / f"{case['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(
            json.dumps(summarize(results), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if not result["complete"]:
            print("Agent turn did not complete; stopping to avoid treating infrastructure failure "
                  "as a memory quality miss.", flush=True)
            break
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="invoke the selected real Agent CLI")
    parser.add_argument("--agent", choices=("codex", "kimi"), default="codex",
                        help="single Agent to evaluate (default: codex)")
    parser.add_argument("--memory-mode", choices=("lexical", "hybrid"),
                        default="lexical", help="recall path to evaluate (default: lexical)")
    parser.add_argument("--prompt-for-kimi-key", action="store_true",
                        help="read Kimi Code key securely from the terminal")
    parser.add_argument("--case", help="run only one case ID (live mode)")
    parser.add_argument("--split", choices=("all", "tuning", "holdout"), default="all",
                        help="run one frozen cohort (default: all)")
    parser.add_argument("--resume-from", type=Path,
                        help="reuse completed prefix from an earlier compatible run")
    parser.add_argument("--output-root", type=Path, default=_RESULTS)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    cases = load_cases()
    validate_cases(cases)
    validate_splits(cases, load_splits())
    if not args.live:
        count = (len(cases) if args.split == "all" else len(load_splits()[args.split]))
        print(f"Validated {count} {args.split} cases; no model called. "
              "Use --live --agent kimi to run Kimi.")
        return
    if args.timeout_seconds < 1:
        parser.error("timeout must be positive")
    if args.prompt_for_kimi_key:
        if args.agent != "kimi":
            parser.error("--prompt-for-kimi-key requires --agent kimi")
        os.environ["KIMI_MODEL_API_KEY"] = getpass.getpass("Kimi Code key: ")
    run_dir = asyncio.run(run_live(
        case_id=args.case, output_root=args.output_root.resolve(),
        timeout_seconds=args.timeout_seconds,
        resume_from=args.resume_from.resolve() if args.resume_from else None,
        agent=args.agent, split=args.split, memory_mode=args.memory_mode,
    ))
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
