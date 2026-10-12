"""Room-scoped chat memory with source-backed automatic Agent proposals."""

import json
import logging
import re
import sqlite3
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.chat.fts_memory import FTS_MEMORY_MIGRATION, MemoryFTSIndex
from app.chat.fts_text import FTSTextLimitError
from app.chat.vector_contracts import MemoryVectorIndexPort
from app.chat.vector_maintenance_schema import VECTOR_MAINTENANCE_MIGRATION
from app.chat.vector_outbox_schema import VECTOR_OUTBOX_MIGRATION
from app.orchestration.models import utc_now
from app.storage import Migration, SQLiteDatabase

CHAT_MEMORY_MIGRATIONS = (
    Migration(
        version=20,
        name="create_standalone_chat_memories",
        statements=(
            """CREATE TABLE standalone_chat_memories (
            memory_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
            source_message_id TEXT NOT NULL REFERENCES standalone_chat_messages(message_id),
            content TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'retired')),
            created_at TEXT NOT NULL,
            retired_at TEXT,
            UNIQUE(room_id, source_message_id, content)
        )""",
            ("CREATE INDEX standalone_chat_memory_room_idx "
             "ON standalone_chat_memories(room_id, status, created_at)"),
        ),
    ),
    Migration(
        version=21,
        name="create_standalone_chat_memory_candidates",
        statements=(
            """CREATE TABLE standalone_chat_memory_candidates (
                candidate_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
                source_message_id TEXT NOT NULL UNIQUE
                    REFERENCES standalone_chat_messages(message_id),
                kind TEXT NOT NULL CHECK(kind IN (
                    'decision', 'correction', 'preference', 'open_loop'
                )),
                proposed_content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                memory_id TEXT REFERENCES standalone_chat_memories(memory_id)
            )""",
            ("CREATE INDEX standalone_chat_candidate_room_idx "
             "ON standalone_chat_memory_candidates(room_id, status, created_at)"),
        ),
    ),
    Migration(
        version=22,
        name="version_standalone_chat_memories",
        statements=(
            ("ALTER TABLE standalone_chat_memories ADD COLUMN kind TEXT NOT NULL "
             "DEFAULT 'fact' CHECK(kind IN ('fact', 'decision', 'correction', "
             "'preference', 'open_loop'))"),
            ("ALTER TABLE standalone_chat_memories ADD COLUMN replaces_memory_id TEXT "
             "REFERENCES standalone_chat_memories(memory_id)"),
            ("ALTER TABLE standalone_chat_memories ADD COLUMN retired_reason TEXT "
             "CHECK(retired_reason IN ('manual', 'replaced'))"),
            ("CREATE UNIQUE INDEX standalone_chat_memory_replacement_idx "
             "ON standalone_chat_memories(replaces_memory_id) "
             "WHERE replaces_memory_id IS NOT NULL"),
        ),
    ),
    Migration(
        version=23,
        name="mark_standalone_chat_memory_origin",
        statements=(
            ("ALTER TABLE standalone_chat_memories ADD COLUMN origin TEXT NOT NULL "
             "DEFAULT 'manual' CHECK(origin IN ('manual', 'agent_auto'))"),
        ),
    ),
    Migration(
        version=24,
        name="create_standalone_chat_memory_embeddings",
        statements=(
            """CREATE TABLE standalone_chat_memory_embeddings (
                memory_id TEXT PRIMARY KEY REFERENCES standalone_chat_memories(memory_id),
                model_fingerprint TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 2 AND 4096),
                vector BLOB NOT NULL
            )""",
            ("CREATE INDEX standalone_chat_embedding_model_idx "
             "ON standalone_chat_memory_embeddings(model_fingerprint)"),
            """CREATE TABLE standalone_chat_embedding_meta (
                model_fingerprint TEXT PRIMARY KEY,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 2 AND 4096)
            )""",
        ),
    ),
    Migration(
        version=25,
        name="close_standalone_chat_open_loops",
        statements=(
            ("ALTER TABLE standalone_chat_memories ADD COLUMN open_loop_state TEXT "
             "NOT NULL DEFAULT 'open' CHECK(open_loop_state IN ('open', 'completed', 'cancelled'))"),
            ("ALTER TABLE standalone_chat_memories ADD COLUMN closed_by_message_id TEXT "
             "REFERENCES standalone_chat_messages(message_id)"),
        ),
    ),
    FTS_MEMORY_MIGRATION,
    VECTOR_OUTBOX_MIGRATION,
    VECTOR_MAINTENANCE_MIGRATION,
)


class ChatMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    memory_id: UUID = Field(default_factory=uuid4)
    room_id: UUID
    source_message_id: UUID
    content: str = Field(min_length=1, max_length=500)
    kind: Literal["fact", "decision", "correction", "preference", "open_loop"] = "fact"
    origin: Literal["manual", "agent_auto"] = "manual"
    replaces_memory_id: UUID | None = None
    status: str = "active"
    created_at: str
    retired_at: str | None = None
    retired_reason: Literal["manual", "replaced"] | None = None
    open_loop_state: Literal["open", "completed", "cancelled"] = "open"
    closed_by_message_id: UUID | None = None


class ChatMemoryCandidate(BaseModel):
    """A detector observation awaiting a local Human's decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    room_id: UUID
    source_message_id: UUID
    kind: Literal["decision", "correction", "preference", "open_loop"]
    proposed_content: str
    status: Literal["pending", "approved", "rejected"]
    created_at: str
    decided_at: str | None = None
    memory_id: UUID | None = None


class AgentMemoryProposal(BaseModel):
    """A read-only Agent's extractive suggestion about its Human source message."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    kind: Literal["decision", "correction", "preference", "open_loop"]
    content: str = Field(min_length=1, max_length=500)
    replaces_memory_id: UUID | None = None


class AgentMemoryAction(BaseModel):
    """A proposed closure grounded in the current local Human message."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    action: Literal["complete", "cancel"]
    memory_id: UUID
    evidence: str = Field(min_length=1, max_length=200)


_WORD = re.compile(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w@])@[\w]+", re.UNICODE)
_SENSITIVE = re.compile(
    r"(?:密码|密钥|私钥|验证码|access[_ -]?token|api[_ -]?key|secret|password)",
    re.IGNORECASE,
)
_THIRD_PARTY_REFERENCE = re.compile(
    r"(?:我(?:的)?(?:朋友|同事|室友|家人|伴侣|父母|孩子)|"
    r"朋友|同事|室友|家人|伴侣|父母|孩子|"
    r"(?:他|她)(?:们)?(?:说|喜欢|偏好|以后|只喝|只吃|不喜欢))"
)
_ROOM_SCOPE_REFERENCE = re.compile(
    r"(?:^|(?<=[，,。；;！!?？：:]))\s*"
    r"在(?:这个|当前)?(?:我(?:和|与))?(?:我的)?"
    r"(?:朋友|同事|室友|家人|伴侣|父母|孩子)"
    r"(?:的)?(?:共享|共用)?(?:的)?(?:聊天)?房间(?:里|内|中)?"
    r"(?=\s*[，,：:])"
)
_DISAVOWED_PREFERENCE = re.compile(
    r"(?:这|那|此)?(?:不是|并非|不代表)(?:我|我的|自己)(?:.{0,8})(?:偏好|喜好|习惯|喜欢)?"
)
_SELF_PREFERENCE_CLAUSE = re.compile(
    r"(?:^|[，,。；;！!?？]|但|而)\s*(?:不过|但是|其实)?\s*"
    r"我(?:自己|其实|个人|更|最|一直|通常|现在|也)*"
    r"(?:喜欢|偏好|不喜欢|不吃|不喝|希望|以后|默认)[^，,。；;！!?？]*"
)
_PROPOSED_THIRD_PARTY_PREFERENCE = re.compile(
    r"(?:朋友|同事|室友|家人|伴侣|父母|孩子|(?:他|她)(?:的|们)?)"
    r".{0,18}(?:喜欢|偏好|喜好|只喝|只吃)"
)
_GENERIC_PREFERENCE_TERMS = frozenset({
    "喜欢", "偏好", "喜好", "用户", "自己", "以后", "默认", "不喜", "欢", "更正",
})
_STABLE_PREFERENCE_INTENT = re.compile(
    r"以后|今后|往后|长期|始终|每次|默认|平时|通常|"
    r"我(?:自己|个人|更|最|一直|通常|也|才|只|不太)*"
    r"(?:喜欢|不喜欢|偏好|习惯|不吃|不喝|接受)|我只在|我的(?:偏好|喜好|习惯)"
)
_ROOM_PREFERENCE_REQUEST = re.compile(
    r"(?:在[^。！？!?；;]{0,32}房间(?:里|内|中)?|(?:本|这个|当前)房间(?:里|内|中)?)"
    r"\s*[，,：:]?\s*(?:请|我希望)"
)
_ONE_SHOT_PREFERENCE = re.compile(
    r"这次|本次|本轮|这轮|这回|仅这条|只这条|当前这条|这条(?:回复|回答)|这(?:一)?(?:杯|顿|餐)"
)
_PREFERENCE_QUESTION = re.compile(r"[？?]|(?:吗|么)\s*[。！!]?\s*$|^(?:是否|要不要|能否|可不可以)")
_PREFERENCE_SENTENCE_END = re.compile(r"[。！？!?；;]")
_MAX_AUTO_MEMORY_QUOTE_CHARS = 200
_CLAUSE_BOUNDARIES = frozenset("，,。！？!?；;：:")
_CONTRAST_WORDS = ("但是", "不过", "然而", "但")
_LIMITING_SUFFIXES = (
    "但是", "不过", "除非", "否则", "只有", "仅在", "仅限", "只在", "只限",
    "如果", "若", "但", "只", "仅", "限", "不", "没", "未",
)
_UNRESOLVED_DECISION = re.compile(
    r"(?:尚未|还没|还未|没有|未|不曾)\s*(?:最终)?(?:决定|确定|选定|敲定|拍板)|"
    r"(?:待定|待选定|待确定|待敲定|待拍板)(?=$|[，,。；;！!?？])"
)
_UNRESOLVED_OPEN_LOOP_ACTION = re.compile(
    r"\s*(?:我们|我|团队)?"
    r"(?:先|接下来|下一步|之后|后续|稍后|下次|请|还需要|需要|还要|要)"
    r"(?:要|需要)?(?:进一步|再)?"
    r"(?:比较|对比|评估|调研|检查|核对|验证|测试|讨论|整理|编写|补充|制定|"
    r"安排|确认|决定|选择|选定|敲定|做|完成)"
)
_ACTION_QUESTION = re.compile(r"[？?]|(?:吗|么)\s*$")
_COMPLETION = re.compile(
    r"(?:已|已经).{0,12}完成|完成了|做完了?|办完了?|处理完了?|解决了?|搞定了?|验收通过"
)
_CANCELLATION = re.compile(r"取消了?|不再做了?|不用做了?|放弃了?")
_UNCERTAIN_CLOSURE = re.compile(
    r"(?:如果|假如|要是|等到|等|计划|打算|准备|希望|可能|还没|尚未|没有|未|别|不要|"
    r"不能|无法|暂不|不是).{0,12}(?:完成|做完|办完|处理完|解决|搞定|取消|放弃)|"
    r"(?:完成|做完|办完|处理完|解决|搞定|取消|放弃)(?:不了|之后|以后|后再|后才|时再)"
)
_LOGGER = logging.getLogger(__name__)
_CANDIDATE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("correction", re.compile(r"更正|纠正|以后不要|不要再|改为|不是.{0,30}而是")),
    ("decision", re.compile(r"我们决定|我决定|团队决定|最终决定|约定为|确定采用|选择采用")),
    ("preference", re.compile(r"我更喜欢|我希望以后|我的偏好|以后请|默认使用|始终使用")),
    ("open_loop", re.compile(r"待办|下次要|之后要|还需要|稍后要")),
)


def detect_candidate(content: str) -> tuple[str, str] | None:
    """Mechanical signal only: this does not assert truth or summarize a chat."""
    normalized = " ".join(_MENTION.sub("", content).split())
    if len(normalized) < 8:
        return None
    for kind, pattern in _CANDIDATE_RULES:
        if pattern.search(normalized):
            excerpt = normalized[:500]
            return kind, excerpt
    return None


def _terms(value: str) -> set[str]:
    """Use ASCII words and Chinese bigrams without an external tokenizer."""
    result: set[str] = set()
    for match in _WORD.finditer(value.casefold()):
        token = match.group()
        if "\u3400" <= token[0] <= "\u9fff":
            result.update(token[index:index + 2] for index in range(len(token) - 1))
        else:
            result.add(token)
    return result


def rank_memory_candidates(
    memories: tuple[ChatMemory, ...], query: str,
    semantic: tuple[tuple[UUID, float], ...] = (), *, limit: int = 3,
) -> tuple[ChatMemory, ...]:
    """Fuse independently retrieved lexical and semantic candidates.

    Kept pure so frozen evaluation cases use the same ranking as live recall.
    ``semantic`` must already be similarity-filtered and sorted by score.
    """
    if not 1 <= limit <= 3:
        raise ValueError("invalid recall limit")
    lexical = lexical_memory_ranking(memories, query)
    return fuse_memory_rankings(
        memories, tuple(memory.memory_id for memory in lexical), semantic, limit=limit,
    )


def lexical_memory_ranking(
    memories: tuple[ChatMemory, ...], query: str,
) -> tuple[ChatMemory, ...]:
    """Frozen legacy branch: overlap count, then recency, with stable ties."""
    terms = _terms(query)
    scored = []
    for memory in memories:
        overlap = terms & _terms(memory.content)
        if overlap:
            scored.append((len(overlap), memory.created_at, memory))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return tuple(item[2] for item in scored)


def fuse_memory_rankings(
    memories: tuple[ChatMemory, ...], lexical_ids: tuple[UUID, ...],
    semantic: tuple[tuple[UUID, float], ...] = (), *, limit: int = 3,
) -> tuple[ChatMemory, ...]:
    """Same RRF=60 and tie policy for legacy and BM25 lexical rankings."""
    if not 1 <= limit <= 3:
        raise ValueError("invalid recall limit")
    by_id = {memory.memory_id: memory for memory in memories}
    lexical_ids = tuple(dict.fromkeys(memory_id for memory_id in lexical_ids if memory_id in by_id))
    if not semantic:
        return tuple(by_id[memory_id] for memory_id in lexical_ids[:limit])
    # Reciprocal rank fusion permits semantic-only memories to enter the result.
    rank_scores: dict[UUID, float] = {}
    lexical_position = {memory_id: index for index, memory_id in enumerate(lexical_ids)}
    for index, memory_id in enumerate(lexical_ids, 1):
        rank_scores[memory_id] = 1 / (60 + index)
    for index, (memory_id, _similarity) in enumerate(semantic, 1):
        if memory_id in by_id:
            rank_scores[memory_id] = rank_scores.get(memory_id, 0.0) + 1 / (60 + index)
    ranked = sorted(
        rank_scores, key=lambda memory_id: (
            -rank_scores[memory_id],
            lexical_position.get(memory_id, 10_000),
            by_id[memory_id].created_at,
            str(memory_id),
        ),
    )
    return tuple(by_id[memory_id] for memory_id in ranked[:limit])


def _auto_preference_has_user_source(
    source_content: str, proposal: AgentMemoryProposal,
) -> bool:
    """Fail closed when a preference proposal may belong to someone else.

    This is a narrow provenance guard, not a general semantic parser. Mixed
    messages are accepted only when the proposed fact overlaps an explicit
    first-person preference clause; uncertain cases remain in chat history.
    """
    if proposal.kind not in {"preference", "correction"}:
        return True
    # A locative such as "在家人共享房间里，" scopes a Human request; it does
    # not attribute the request to family members. Ignore only this explicit
    # syntax for attribution checks, preserving the original stored quote.
    source = _ROOM_SCOPE_REFERENCE.sub("", _MENTION.sub("", source_content))
    proposed = _ROOM_SCOPE_REFERENCE.sub("", proposal.content)
    if not (_THIRD_PARTY_REFERENCE.search(source)
            or _DISAVOWED_PREFERENCE.search(source)):
        return True
    own_clauses = _SELF_PREFERENCE_CLAUSE.findall(source)
    if not own_clauses or _PROPOSED_THIRD_PARTY_PREFERENCE.search(proposed):
        return False
    own_terms = set().union(*(_terms(clause) for clause in own_clauses))
    specific_overlap = (_terms(proposal.content) & own_terms) - _GENERIC_PREFERENCE_TERMS
    return bool(specific_overlap)


def _auto_preference_has_stable_intent(source_content: str, content: str) -> bool:
    """A quoted answer instruction is not automatically a lasting preference.

    Require an explicit personal habit/future/default or room-scoped request.
    Inspect the sentence around the selected span so dropping a question mark
    or a one-shot prefix cannot promote it. Other sentences may still ask a
    question; they do not erase an independently stated lasting preference.
    This is conservative lexical evidence, not a general intent classifier.
    """
    if not (_STABLE_PREFERENCE_INTENT.search(content) or _ROOM_PREFERENCE_REQUEST.search(content)):
        return False
    source = " ".join(_MENTION.sub("", source_content).split())
    start = source.find(content)
    if start < 0:
        return False
    boundaries = tuple(_PREFERENCE_SENTENCE_END.finditer(source[:start]))
    beginning = boundaries[-1].end() if boundaries else 0
    end = start + len(content)
    if content[-1:] not in "。！？!?；;":
        boundary = _PREFERENCE_SENTENCE_END.search(source, end)
        end = boundary.end() if boundary else len(source)
    sentence = source[beginning:end].strip()
    return not (_ONE_SHOT_PREFERENCE.search(sentence) or _PREFERENCE_QUESTION.search(sentence)
                or _UNRESOLVED_DECISION.search(sentence))


def _auto_open_loop_has_action(proposal: AgentMemoryProposal) -> bool:
    """An undecided status alone is not an actionable open loop.

    Keep this guard narrow: it checks unresolved selections, not every possible
    task wording. The quoted proposal itself must include an explicit action,
    rather than relying on the model's suggestions or an omitted source clause.
    """
    if proposal.kind != "open_loop" or not _UNRESOLVED_DECISION.search(proposal.content):
        return True
    return any(
        _UNRESOLVED_OPEN_LOOP_ACTION.match(clause) and not _ACTION_QUESTION.search(clause)
        for clause in re.split(r"[，,。；;！!]", proposal.content)
    )


def _grounded_auto_memory_content(source_content: str, proposed_content: str) -> str | None:
    """Keep only a short verbatim span of the current Human message.

    A model-selected source span is less expressive than a free-form summary,
    but a local substring check can reject added advice, unsupported decisions,
    and calendar dates inferred from relative time without another model call.
    Whitespace and the routing mention are not part of the remembered fact.
    """
    source = " ".join(_MENTION.sub("", source_content).split())
    content = " ".join(proposed_content.split())
    if (not content or len(content) > _MAX_AUTO_MEMORY_QUOTE_CHARS
            or content not in source):
        return None
    # A substring can invert a claim by dropping its prefix: "不喜欢香菜"
    # contains "喜欢香菜". Require the span to begin at a clause boundary (or
    # after an explicit contrast), and do not drop an immediately following
    # condition or negation. This remains a conservative heuristic, not a
    # semantic entailment checker.
    start = source.find(content)
    while start >= 0:
        before = source[:start].rstrip()
        starts_at_clause = (not before or before[-1] in _CLAUSE_BOUNDARIES)
        if not starts_at_clause:
            starts_at_clause = any(
                before.endswith(word)
                and before[:-len(word)].rstrip()[-1:] in _CLAUSE_BOUNDARIES
                for word in _CONTRAST_WORDS
            )
        after = source[start + len(content):].lstrip(" ，,")
        if starts_at_clause and not after.startswith(_LIMITING_SUFFIXES):
            return content
        start = source.find(content, start + 1)
    return None


def suggest_open_loop_closure_phrase(
    content: str, action: Literal["complete", "cancel"],
) -> str | None:
    """Offer only a short example that passes the same local evidence checks.

The source still needs to be a fresh local Human message, and the target must
remain the unique active lexical match when the example is actually sent.
    """
    ending = "这项待办已完成" if action == "complete" else "这项待办取消了"
    phrase = f"关于待办「{content}」，{ending}。"
    expected = _COMPLETION if action == "complete" else _CANCELLATION
    opposite = _CANCELLATION if action == "complete" else _COMPLETION
    if (len(phrase) > _MAX_AUTO_MEMORY_QUOTE_CHARS or _SENSITIVE.search(phrase)
            or _UNCERTAIN_CLOSURE.search(phrase) or "?" in phrase or "？" in phrase
            or not expected.search(phrase) or opposite.search(phrase)
            or len(_terms(phrase) & _terms(content)) < 2
            or _grounded_auto_memory_content(phrase, phrase) is None):
        return None
    return phrase


class ChatMemoryStore:
    def __init__(
        self, database: SQLiteDatabase, *, vector_index: MemoryVectorIndexPort | None = None,
        lexical_backend: Literal["legacy", "bm25"] = "legacy",
    ) -> None:
        if lexical_backend not in ("legacy", "bm25"):
            raise ValueError("invalid memory lexical backend")
        self.database = database
        self.vector_index = vector_index
        self.lexical_backend = lexical_backend
        self.fts_index = MemoryFTSIndex(database)

    def capture_agent_proposal(
        self, room_id: UUID, source_message_id: UUID, proposal: AgentMemoryProposal,
    ) -> ChatMemory | None:
        """Auto-promote at most one proposal per local Human source message.

        A rejected historical candidate remains rejected. This method is called
        only after a successful Agent reply; its transaction makes the proposal
        and resulting memory atomic and safe to repeat after an uncertain result.
        """
        with self.database.transaction() as connection:
            source = connection.execute(
                "SELECT m.message_json, member.role FROM standalone_chat_messages AS m "
                "JOIN standalone_chat_members AS member ON member.member_id = m.sender_id "
                "WHERE m.room_id = ? AND m.message_id = ?",
                (str(room_id), str(source_message_id)),
            ).fetchone()
            if source is None or source["role"] != "human":
                return None
            # External sources are never promoted into the local room memory.
            message = json.loads(source["message_json"])
            content = _grounded_auto_memory_content(
                message["content"], proposal.content,
            )
            if (message.get("external_source") is not None
                    or _SENSITIVE.search(message["content"])
                    or content is None
                    or _SENSITIVE.search(content)
                    or (proposal.kind == "decision" and _UNRESOLVED_DECISION.search(content))
                    or (proposal.kind == "preference" and not _auto_preference_has_stable_intent(message["content"], content))
                    or not _auto_open_loop_has_action(proposal)
                    or not _auto_preference_has_user_source(message["content"], proposal)):
                return None
            root = connection.execute(
                "SELECT message_json FROM standalone_chat_messages "
                "WHERE correlation_id = ? ORDER BY sequence LIMIT 1",
                (message["correlation_id"],),
            ).fetchone()
            if root is None or json.loads(root["message_json"]).get("external_source") is not None:
                return None
            candidate = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates "
                "WHERE source_message_id = ?",
                (str(source_message_id),),
            ).fetchone()
            if candidate is not None:
                if candidate["status"] == "rejected":
                    return None
                if candidate["status"] == "approved":
                    self.fts_index.sync(connection, UUID(candidate["memory_id"]))
                    return self._get(connection, UUID(candidate["memory_id"]))
            try:
                memory = self._create(
                    connection, room_id, source_message_id, content,
                    kind=proposal.kind, replaces_memory_id=proposal.replaces_memory_id,
                    origin="agent_auto",
                )
            except ValueError:
                return None
            now = utc_now().isoformat()
            if candidate is None:
                connection.execute(
                    """INSERT INTO standalone_chat_memory_candidates
                    (candidate_id, room_id, source_message_id, kind, proposed_content,
                     status, created_at, decided_at, memory_id)
                    VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?)""",
                    (str(uuid5(source_message_id, "codecrew:memory-candidate-v1")),
                     str(room_id), str(source_message_id), proposal.kind, content,
                     now, now, str(memory.memory_id)),
                )
            else:
                connection.execute(
                    "UPDATE standalone_chat_memory_candidates SET status = 'approved', "
                    "proposed_content = ?, kind = ?, decided_at = ?, memory_id = ? "
                    "WHERE candidate_id = ?",
                    (content, proposal.kind, now, str(memory.memory_id),
                     candidate["candidate_id"]),
                )
        self._index_if_enabled(memory)
        return memory

    def create(
        self, room_id: UUID, source_message_id: UUID, content: str,
        *, replaces_memory_id: UUID | None = None,
    ) -> ChatMemory:
        content = " ".join(content.split())
        if not 1 <= len(content) <= 500:
            raise ValueError("memory content must be 1–500 characters")
        with self.database.transaction() as connection:
            memory = self._create(
                connection, room_id, source_message_id, content,
                kind="fact", replaces_memory_id=replaces_memory_id,
            )
            connection.execute(
                "UPDATE standalone_chat_memory_candidates SET status = 'approved', "
                "decided_at = COALESCE(decided_at, ?), memory_id = ? "
                "WHERE room_id = ? AND source_message_id = ? AND status = 'pending'",
                (utc_now().isoformat(), str(memory.memory_id),
                 str(room_id), str(source_message_id)),
            )
        self._index_if_enabled(memory)
        return memory

    def _index_if_enabled(self, memory: ChatMemory) -> None:
        if self.vector_index is None or _SENSITIVE.search(memory.content):
            return
        try:
            self.vector_index.index((memory,))
        except Exception as exc:  # noqa: BLE001 - derived index must not block canonical write
            _LOGGER.warning("memory vector indexing failed: %s", type(exc).__name__)

    def _create(
        self, connection: sqlite3.Connection, room_id: UUID,
        source_message_id: UUID, content: str, *,
        kind: Literal["fact", "decision", "correction", "preference", "open_loop"],
        replaces_memory_id: UUID | None,
        origin: Literal["manual", "agent_auto"] = "manual",
    ) -> ChatMemory:
        if replaces_memory_id is not None:
            replacement = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE replaces_memory_id = ?",
                (str(replaces_memory_id),),
            ).fetchone()
            if replacement is not None:
                existing = self._from_row(replacement)
                if (existing.room_id != room_id or
                        existing.source_message_id != source_message_id or
                        existing.content != content or existing.kind != kind
                        or existing.origin != origin):
                    raise ValueError("memory was already replaced by different content")
                if existing.status != "active":
                    raise ValueError("retired memory cannot be recreated implicitly")
                self.fts_index.sync(connection, existing.memory_id)
                return existing
            old = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? AND memory_id = ?",
                (str(room_id), str(replaces_memory_id)),
            ).fetchone()
            if old is None or old["status"] != "active":
                raise ValueError("replacement source must be an active memory in this room")
        row = connection.execute(
            "SELECT memory_id FROM standalone_chat_memories "
            "WHERE room_id = ? AND source_message_id = ? AND content = ?",
            (str(room_id), str(source_message_id), content),
        ).fetchone()
        if row is not None:
            existing = self._get(connection, UUID(row["memory_id"]))
            if existing.status == "retired":
                raise ValueError("retired memory cannot be recreated implicitly")
            if (existing.replaces_memory_id != replaces_memory_id or existing.kind != kind
                    or existing.origin != origin):
                raise ValueError("memory already exists with different lineage or kind")
            self.fts_index.sync(connection, existing.memory_id)
            return existing
        active_count = connection.execute(
            "SELECT COUNT(*) AS total FROM standalone_chat_memories "
            "WHERE room_id = ? AND status = 'active'",
            (str(room_id),),
        ).fetchone()["total"]
        if active_count >= 200 and replaces_memory_id is None:
            raise ValueError("room memory limit of 200 active items reached")
        memory = ChatMemory(
            room_id=room_id, source_message_id=source_message_id,
            content=content, kind=kind, origin=origin,
            replaces_memory_id=replaces_memory_id,
            created_at=utc_now().isoformat(),
        )
        connection.execute(
            """INSERT INTO standalone_chat_memories
            (memory_id, room_id, source_message_id, content, kind, origin,
             replaces_memory_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (str(memory.memory_id), str(room_id), str(source_message_id),
             content, kind, origin,
             str(replaces_memory_id) if replaces_memory_id else None,
             memory.created_at),
        )
        if replaces_memory_id is not None:
            connection.execute(
                "UPDATE standalone_chat_memories SET status = 'retired', "
                "retired_at = ?, retired_reason = 'replaced' WHERE memory_id = ?",
                (memory.created_at, str(replaces_memory_id)),
            )
            connection.execute(
                "DELETE FROM standalone_chat_memory_embeddings WHERE memory_id = ?",
                (str(replaces_memory_id),),
            )
        self.fts_index.sync(
            connection, memory.memory_id,
            *((replaces_memory_id,) if replaces_memory_id is not None else ()),
        )
        return memory

    def observe_message(
        self, connection: sqlite3.Connection, room_id: UUID,
        source_message_id: UUID, content: str,
    ) -> ChatMemoryCandidate | None:
        """Called in the message's transaction; pending candidates are never recalled."""
        detected = detect_candidate(content)
        if detected is None:
            return None
        existing = connection.execute(
            "SELECT * FROM standalone_chat_memory_candidates WHERE source_message_id = ?",
            (str(source_message_id),),
        ).fetchone()
        if existing is not None:
            return self._candidate_from_row(existing)
        pending_count = connection.execute(
            "SELECT COUNT(*) AS total FROM standalone_chat_memory_candidates "
            "WHERE room_id = ? AND status = 'pending'",
            (str(room_id),),
        ).fetchone()["total"]
        if pending_count >= 100:
            return None
        kind, proposed_content = detected
        candidate_id = uuid5(source_message_id, "codecrew:memory-candidate-v1")
        created_at = utc_now().isoformat()
        connection.execute(
            """INSERT INTO standalone_chat_memory_candidates
            (candidate_id, room_id, source_message_id, kind, proposed_content,
             status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (str(candidate_id), str(room_id), str(source_message_id), kind,
             proposed_content, created_at),
        )
        return ChatMemoryCandidate(
            candidate_id=candidate_id, room_id=room_id,
            source_message_id=source_message_id, kind=kind,
            proposed_content=proposed_content, status="pending",
            created_at=created_at,
        )

    def list_pending(self, room_id: UUID) -> tuple[ChatMemoryCandidate, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates "
                "WHERE room_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC, candidate_id DESC LIMIT 100",
                (str(room_id),),
            ).fetchall()
            return tuple(self._candidate_from_row(row) for row in rows)

    def approve_candidate(
        self, room_id: UUID, candidate_id: UUID, content: str,
        *, replaces_memory_id: UUID | None = None,
    ) -> tuple[ChatMemoryCandidate, ChatMemory] | None:
        content = " ".join(content.split())
        if not 1 <= len(content) <= 500:
            raise ValueError("memory content must be 1–500 characters")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates "
                "WHERE room_id = ? AND candidate_id = ?",
                (str(room_id), str(candidate_id)),
            ).fetchone()
            if row is None:
                return None
            candidate = self._candidate_from_row(row)
            if candidate.status == "rejected":
                raise ValueError("rejected candidate cannot be approved")
            if candidate.status == "approved":
                if candidate.memory_id is None:
                    raise ValueError("approved candidate has no memory")
                memory = self._get(connection, candidate.memory_id)
                if memory.content != content or memory.replaces_memory_id != replaces_memory_id:
                    raise ValueError("approved candidate has different content or lineage")
                self.fts_index.sync(connection, memory.memory_id)
                return candidate, memory
            memory = self._create(
                connection, room_id, candidate.source_message_id, content,
                kind=candidate.kind, replaces_memory_id=replaces_memory_id,
            )
            connection.execute(
                "UPDATE standalone_chat_memory_candidates SET status = 'approved', "
                "decided_at = ?, memory_id = ? WHERE candidate_id = ?",
                (utc_now().isoformat(), str(memory.memory_id), str(candidate_id)),
            )
            final = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            approved = self._candidate_from_row(final)
        self._index_if_enabled(memory)
        return approved, memory

    def reject_candidate(
        self, room_id: UUID, candidate_id: UUID,
    ) -> ChatMemoryCandidate | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates "
                "WHERE room_id = ? AND candidate_id = ?",
                (str(room_id), str(candidate_id)),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "approved":
                raise ValueError("approved candidate cannot be rejected; retire its memory")
            if row["status"] == "pending":
                connection.execute(
                    "UPDATE standalone_chat_memory_candidates SET status = 'rejected', "
                    "decided_at = ? WHERE candidate_id = ?",
                    (utc_now().isoformat(), str(candidate_id)),
                )
            final = connection.execute(
                "SELECT * FROM standalone_chat_memory_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            return self._candidate_from_row(final)

    def list_active(self, room_id: UUID, *, limit: int = 200) -> tuple[ChatMemory, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("invalid memory limit")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? "
                "AND status = 'active' ORDER BY created_at DESC, memory_id DESC LIMIT ?",
                (str(room_id), limit),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)

    def list_closed_open_loops(
        self, room_id: UUID, *, limit: int = 50,
    ) -> tuple[ChatMemory, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("invalid closed open-loop limit")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? "
                "AND kind = 'open_loop' AND open_loop_state != 'open' "
                "ORDER BY retired_at DESC, memory_id DESC LIMIT ?",
                (str(room_id), limit),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)

    def lineage(
        self, room_id: UUID, memory_id: UUID, *, limit: int = 20,
    ) -> tuple[tuple[ChatMemory, ...], bool] | None:
        """Follow version links from current to oldest, never across rooms."""
        if not 1 <= limit <= 50:
            raise ValueError("invalid memory lineage limit")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? AND memory_id = ?",
                (str(room_id), str(memory_id)),
            ).fetchone()
            if row is None:
                return None
            items: list[ChatMemory] = []
            seen: set[UUID] = set()
            while row is not None and len(items) < limit:
                memory = self._from_row(row)
                if memory.memory_id in seen:
                    raise ValueError("memory lineage contains a cycle")
                seen.add(memory.memory_id)
                items.append(memory)
                row = (connection.execute(
                    "SELECT * FROM standalone_chat_memories WHERE room_id = ? "
                    "AND memory_id = ?",
                    (str(room_id), str(memory.replaces_memory_id)),
                ).fetchone() if memory.replaces_memory_id else None)
            return tuple(items), row is not None

    def retrieve(
        self, room_id: UUID, query: str, *, limit: int = 3,
        exclude_correlation_id: UUID | None = None,
    ) -> tuple[ChatMemory, ...]:
        """Bounded opt-in BM25/legacy recall with optional vector RRF."""
        if not 1 <= limit <= 3:
            raise ValueError("invalid recall limit")
        if self.lexical_backend == "legacy" and not _terms(query) and self.vector_index is None:
            return ()
        lexical_ids = None
        fts_error = None
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT memory.* FROM standalone_chat_memories AS memory "
                "JOIN standalone_chat_messages AS source "
                "ON source.message_id = memory.source_message_id "
                "WHERE memory.room_id = ? AND memory.status = 'active' "
                "AND (? IS NULL OR source.correlation_id != ?) "
                "ORDER BY memory.created_at DESC, memory.memory_id DESC LIMIT 200",
                (str(room_id), str(exclude_correlation_id) if exclude_correlation_id else None,
                 str(exclude_correlation_id) if exclude_correlation_id else None),
            ).fetchall()
            memories = tuple(self._from_row(row) for row in rows)
            if self.lexical_backend == "bm25":
                try:
                    lexical_ids = self.fts_index.search(
                        connection, room_id, query,
                        memory_ids=tuple(memory.memory_id for memory in memories),
                        exclude_correlation_id=exclude_correlation_id,
                    )
                except FTSTextLimitError:
                    # Bad/oversized query is not index corruption. Do not mark dirty.
                    _LOGGER.warning("memory BM25 query degraded: FTSTextLimitError")
                except sqlite3.Error as exc:
                    fts_error = type(exc).__name__
        if fts_error is not None:
            _LOGGER.warning("memory BM25 query degraded: %s", fts_error)
            try:
                self.fts_index.invalidate(fts_error)
            except sqlite3.Error as exc:
                # Still return canonical legacy results; subsequent FTS reads must
                # re-check state and handle errors, not cache a trusted-ready flag.
                _LOGGER.warning("memory FTS invalidation failed: %s", type(exc).__name__)
        if lexical_ids is None:
            lexical_ids = tuple(memory.memory_id for memory in lexical_memory_ranking(memories, query))
        if self.vector_index is None or _SENSITIVE.search(query):
            ranked = fuse_memory_rankings(memories, lexical_ids, limit=limit)
            return self._validate_recall(room_id, ranked, exclude_correlation_id)
        safe_memories = tuple(memory for memory in memories
                              if not _SENSITIVE.search(memory.content))
        try:
            scoped_search = getattr(self.vector_index, "search_scoped", None)
            clean_query = _MENTION.sub("", query).strip()
            if scoped_search is not None:
                semantic = scoped_search(clean_query, safe_memories, room_id=room_id,
                                         exclude_correlation_id=exclude_correlation_id)
            else:
                semantic = self.vector_index.search(clean_query, safe_memories)
        except Exception as exc:  # noqa: BLE001 - cloud/vector failure is lexical-only
            _LOGGER.warning("memory vector recall degraded: %s", type(exc).__name__)
            semantic = ()
        ranked = fuse_memory_rankings(memories, lexical_ids, semantic, limit=limit)
        return self._validate_recall(room_id, ranked, exclude_correlation_id)

    def _validate_recall(
        self, room_id: UUID, ranked: tuple[ChatMemory, ...],
        exclude_correlation_id: UUID | None,
    ) -> tuple[ChatMemory, ...]:
        # Cloud work happens outside the snapshot. Recheck before returning so
        # a concurrent retirement/closure cannot leak its old in-memory record.
        if not ranked:
            return ()
        placeholders = ",".join("?" for _ in ranked)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT memory.* FROM standalone_chat_memories memory "
                "JOIN standalone_chat_messages source ON source.message_id = memory.source_message_id "
                "WHERE memory.room_id = ? AND memory.status = 'active' "
                "AND (? IS NULL OR source.correlation_id != ?) "
                f"AND memory.memory_id IN ({placeholders})",
                (str(room_id), str(exclude_correlation_id) if exclude_correlation_id else None,
                 str(exclude_correlation_id) if exclude_correlation_id else None,
                 *(str(memory.memory_id) for memory in ranked)),
            ).fetchall()
        current = {UUID(row["memory_id"]): self._from_row(row) for row in rows}
        return tuple(current[memory.memory_id] for memory in ranked
                     if current.get(memory.memory_id) == memory)

    def retire(self, room_id: UUID, memory_id: UUID) -> ChatMemory | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_memories "
                "WHERE room_id = ? AND memory_id = ?",
                (str(room_id), str(memory_id)),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "active":
                connection.execute(
                    "UPDATE standalone_chat_memories SET status = 'retired', "
                    "retired_at = ?, retired_reason = 'manual' WHERE memory_id = ?",
                    (utc_now().isoformat(), str(memory_id)),
                )
                connection.execute(
                    "DELETE FROM standalone_chat_memory_embeddings WHERE memory_id = ?",
                    (str(memory_id),),
                )
            self.fts_index.sync(connection, memory_id)
            return self._get(connection, memory_id)

    def close_open_loop(
        self, room_id: UUID, memory_id: UUID, *, action: Literal["complete", "cancel"],
        source_message_id: UUID | None = None,
    ) -> ChatMemory | None:
        """Close an active todo. Same-action retries are idempotent; opposite actions conflict."""
        state = "completed" if action == "complete" else "cancelled"
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? AND memory_id = ?",
                (str(room_id), str(memory_id)),
            ).fetchone()
            if row is None:
                return None
            if row["kind"] != "open_loop":
                raise ValueError("only open_loop memory can be closed")
            if row["open_loop_state"] == state:
                self.fts_index.sync(connection, memory_id)
                return self._from_row(row)
            if row["status"] != "active" or row["open_loop_state"] != "open":
                raise ValueError("open_loop was already retired or closed differently")
            if source_message_id is not None:
                source = connection.execute(
                    "SELECT m.message_json, member.role FROM standalone_chat_messages AS m "
                    "JOIN standalone_chat_members AS member ON member.member_id = m.sender_id "
                    "WHERE m.room_id = ? AND m.message_id = ?",
                    (str(room_id), str(source_message_id)),
                ).fetchone()
                if source is None or source["role"] != "human":
                    raise ValueError("closure source must be a local Human message")
                message = json.loads(source["message_json"])
                if message.get("external_source") is not None:
                    raise ValueError("external message cannot close local memory")
                root = connection.execute(
                    "SELECT message_json FROM standalone_chat_messages "
                    "WHERE correlation_id = ? ORDER BY sequence LIMIT 1",
                    (message["correlation_id"],),
                ).fetchone()
                if root is None or json.loads(root["message_json"]).get("external_source"):
                    raise ValueError("external discussion cannot close local memory")
            connection.execute(
                "UPDATE standalone_chat_memories SET status = 'retired', "
                "open_loop_state = ?, retired_at = ?, closed_by_message_id = ? "
                "WHERE memory_id = ?",
                (state, utc_now().isoformat(), str(source_message_id) if source_message_id else None,
                 str(memory_id)),
            )
            connection.execute(
                "DELETE FROM standalone_chat_memory_embeddings WHERE memory_id = ?",
                (str(memory_id),),
            )
            self.fts_index.sync(connection, memory_id)
            return self._get(connection, memory_id)

    def capture_agent_action(
        self, room_id: UUID, source_message_id: UUID, action: AgentMemoryAction,
    ) -> ChatMemory | None:
        """Conservatively accept an exact, unambiguous Human closure statement."""
        with self.database.connect() as connection:
            source = connection.execute(
                "SELECT m.message_json, member.role FROM standalone_chat_messages AS m "
                "JOIN standalone_chat_members AS member ON member.member_id = m.sender_id "
                "WHERE m.room_id = ? AND m.message_id = ?",
                (str(room_id), str(source_message_id)),
            ).fetchone()
            if source is None or source["role"] != "human":
                return None
            message = json.loads(source["message_json"])
            evidence = _grounded_auto_memory_content(message["content"], action.evidence)
            if (message.get("external_source") is not None or evidence is None
                    or _SENSITIVE.search(message["content"])
                    or _UNCERTAIN_CLOSURE.search(evidence)
                    or "?" in evidence or "？" in evidence):
                return None
            root = connection.execute(
                "SELECT message_json FROM standalone_chat_messages "
                "WHERE correlation_id = ? ORDER BY sequence LIMIT 1",
                (message["correlation_id"],),
            ).fetchone()
            if root is None or json.loads(root["message_json"]).get("external_source"):
                return None
            expected = _COMPLETION if action.action == "complete" else _CANCELLATION
            opposite = _CANCELLATION if action.action == "complete" else _COMPLETION
            if not expected.search(evidence) or opposite.search(evidence):
                return None
            target = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? AND memory_id = ?",
                (str(room_id), str(action.memory_id)),
            ).fetchone()
            closed_state = "completed" if action.action == "complete" else "cancelled"
            if (target is not None and target["kind"] == "open_loop"
                    and target["open_loop_state"] == closed_state
                    and target["closed_by_message_id"] == str(source_message_id)):
                return self._from_row(target)
            rows = connection.execute(
                "SELECT * FROM standalone_chat_memories WHERE room_id = ? "
                "AND kind = 'open_loop' AND status = 'active'",
                (str(room_id),),
            ).fetchall()
            scores = {
                UUID(row["memory_id"]): len(_terms(evidence) & _terms(row["content"]))
                for row in rows
            }
            score = scores.get(action.memory_id, 0)
            if score < 2 or sum(value >= score for value in scores.values()) != 1:
                return None
        try:
            return self.close_open_loop(
                room_id, action.memory_id, action=action.action,
                source_message_id=source_message_id,
            )
        except ValueError:
            return None

    @classmethod
    def _get(cls, connection, memory_id: UUID) -> ChatMemory:
        row = connection.execute(
            "SELECT * FROM standalone_chat_memories WHERE memory_id = ?",
            (str(memory_id),),
        ).fetchone()
        return cls._from_row(row)

    @staticmethod
    def _from_row(row) -> ChatMemory:
        return ChatMemory(
            memory_id=UUID(row["memory_id"]), room_id=UUID(row["room_id"]),
            source_message_id=UUID(row["source_message_id"]), content=row["content"],
            kind=row["kind"], origin=row["origin"],
            replaces_memory_id=(UUID(row["replaces_memory_id"])
                                if row["replaces_memory_id"] else None),
            status=row["status"], created_at=row["created_at"],
            retired_at=row["retired_at"], retired_reason=row["retired_reason"],
            open_loop_state=row["open_loop_state"],
            closed_by_message_id=(UUID(row["closed_by_message_id"])
                                  if row["closed_by_message_id"] else None),
        )

    @staticmethod
    def _candidate_from_row(row) -> ChatMemoryCandidate:
        return ChatMemoryCandidate(
            candidate_id=UUID(row["candidate_id"]), room_id=UUID(row["room_id"]),
            source_message_id=UUID(row["source_message_id"]),
            kind=row["kind"], proposed_content=row["proposed_content"],
            status=row["status"], created_at=row["created_at"],
            decided_at=row["decided_at"],
            memory_id=UUID(row["memory_id"]) if row["memory_id"] else None,
        )
