"""Text contract and actual in-memory FTS5 behavior; no model or user database."""

import re
import sqlite3
from contextlib import closing

import pytest

from app.chat.fts_text import (
    FTS_TEXT_REVISION,
    FTS_TEXT_VERSION,
    FTS_TOKENIZER,
    MAX_QUERY_CHARS,
    MAX_QUERY_TOKENS,
    MAX_TEXT_CHARS,
    FTSTextLimitError,
    FTSToken,
    build_fts_query,
    encode_fts_document,
    tokenize_fts_text,
)


@pytest.fixture
def fts():
    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.execute(
                f"CREATE VIRTUAL TABLE terms USING fts5(content, tokenize='{FTS_TOKENIZER}')"
            )
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" in str(exc):
                pytest.skip("this runtime lacks FTS5; readiness command reports unavailable")
            raise
        yield connection


def _search(connection, documents: list[str], query: str) -> list[int]:
    connection.executemany("INSERT INTO terms(rowid, content) VALUES (?, ?)", [
        (index, encode_fts_document(text)) for index, text in enumerate(documents, 1)
    ])
    match = build_fts_query(query)
    if match is None:
        return []
    return [row[0] for row in connection.execute(
        "SELECT rowid FROM terms WHERE terms MATCH ? ORDER BY bm25(terms), rowid", (match,),
    )]


def test_two_han_character_terms_are_overlapping_without_crossing_boundaries() -> None:
    assert tokenize_fts_text("数据库，超时 API网关") == (
        FTSToken("h", "数据"), FTSToken("h", "据库"), FTSToken("h", "超时"),
        FTSToken("w", "api"), FTSToken("h", "网关"),
    )
    assert FTSToken("h", "库超") not in tokenize_fts_text("数据库，超时")
    assert tokenize_fts_text("库 秒 😀") == ()


def test_supplementary_han_characters_are_supported() -> None:
    assert tokenize_fts_text("𠮷野") == (FTSToken("h", "𠮷野"),)


@pytest.mark.parametrize("plain,variant", [
    ("API RabbitMQ", "ＡＰＩ ＲａｂｂｉｔＭＱ"),
    ("C++ C#", "Ｃ＋＋ Ｃ＃"),
    ("8.5 v1.2.3", "８．５ Ｖ１．２．３"),
])
def test_nfkc_casefold_is_identical_for_documents_and_queries(plain, variant) -> None:
    assert encode_fts_document(plain) == encode_fts_document(variant)
    assert build_fts_query(plain) == build_fts_query(variant)


def test_documents_preserve_frequency_but_queries_deduplicate() -> None:
    encoded = FTSToken("h", "超时").encoded
    assert encode_fts_document("超时 超时 超时").split() == [encoded] * 3
    assert build_fts_query("超时 超时") == f'"{encoded}"'


def test_mentions_removed_only_from_leading_query_not_document() -> None:
    assert build_fts_query(" @白金 @月见 网关") == build_fts_query("网关")
    assert build_fts_query("@白金") is None
    assert FTSToken("h", "白金") in tokenize_fts_text("@白金 网关")


@pytest.mark.parametrize("query", ["", "  ", "😀", "* ^ - + ()", "库", "秒"])
def test_nonsearchable_queries_never_become_unfiltered_search(query) -> None:
    assert build_fts_query(query) is None


@pytest.mark.parametrize("text", [
    'RabbitMQ OR NOT "*" NEAR(网关)',
    "content:网关* -SQLite ^8.5",
    "'; DROP TABLE terms; --",
    "C++ /api/v1 memory_embedding_mode",
])
def test_query_contains_only_generated_literal_tokens_and_or(text) -> None:
    query = build_fts_query(text)
    assert query is not None
    assert re.fullmatch(r'"[hntw][0-9a-f]+"(?: OR "[hntw][0-9a-f]+")*', query)
    assert all(token.encoded.isascii() and token.encoded.isalnum()
               for token in tokenize_fts_text(text))


@pytest.mark.parametrize("documents,query,expected", [
    (["网关读超时统一定为8秒", "日志保存三天"], "超时", [1]),
    (["每次上线先检查文档链接", "按天归档日志"], "上线", [1]),
    (["API网关采用RabbitMQ", "数据库使用SQLite"], "ＲＡＢＢＩＴＭＱ", [1]),
    (["实现使用 C++", "实现使用 C#", "实现使用 C"], "C++", [1]),
    (["实现使用 C++", "实现使用 C#", "实现使用 C"], "C#", [2]),
    (["实现使用 C++", "实现使用 C#", "实现使用 C"], "C", [3]),
    (["版本 v1.2.3", "版本 v1.2.4"], "v1.2.3", [1]),
    (["超时 8.5 秒", "超时 8 秒", "超时 80 秒"], "8.5", [1]),
    (["超时 8.5 秒", "超时 8 秒", "超时 80 秒"], "8", [2]),
    (["偏移 -8", "偏移 8", "偏移 +8"], "-8", [1]),
    (["端口8080", "端口80"], "8080", [1]),
    (["超时8ms", "超时80ms"], "8", [1]),
    (["配置 memory_embedding_mode", "配置 memory_mode"], "embedding", [1]),
    (["路由 /api/v1", "路由 /chat/v2"], "/api/v1", [1]),
    (["配置 .env", "配置 .gitignore"], ".env", [1]),
    (["工具用 RabbitMQ", "工具用 Redis"], 'RabbitMQ" OR *', [1]),
    (["工具用 RabbitMQ", "工具用 Redis"], '"*"', []),
    (["工具用 RabbitMQ", "工具用 Redis"], "库", []),
])
def test_actual_fts5_literal_matching(fts, documents, query, expected) -> None:
    assert _search(fts, documents, query) == expected


def test_sql_and_fts_operators_are_data_and_do_not_drop_table(fts) -> None:
    assert _search(fts, ["网关超时", "日志保存"], "'; DROP TABLE terms; --") == []
    assert fts.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 2


def test_default_unicode61_and_trigram_miss_two_han_short_term(fts) -> None:
    fts.execute("CREATE VIRTUAL TABLE native USING fts5(content)")
    fts.execute("CREATE VIRTUAL TABLE tri USING fts5(content, tokenize='trigram')")
    for table in ("native", "tri"):
        fts.execute(f"INSERT INTO {table}(content) VALUES (?)", ("网关读超时统一为8秒",))
        assert fts.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ?", ('"超时"',),
        ).fetchall() == []
    assert _search(fts, ["网关读超时统一为8秒"], "超时") == [1]


def test_bm25_can_use_occurrence_frequency_not_a_deduplicated_document(fts) -> None:
    assert _search(fts, ["超时 超时 超时", "超时 日志 配置"], "超时") == [1, 2]
    scores = fts.execute(
        "SELECT rowid, bm25(terms) FROM terms WHERE terms MATCH ? ORDER BY bm25(terms)",
        (build_fts_query("超时"),),
    ).fetchall()
    assert scores[0][1] < scores[1][1]


@pytest.mark.parametrize("function,text", [
    (encode_fts_document, "a" * (MAX_TEXT_CHARS + 1)),
    (encode_fts_document, "ﬃ" * MAX_TEXT_CHARS),
    (build_fts_query, "a" * (MAX_QUERY_CHARS + 1)),
    (build_fts_query, "ﬃ" * MAX_QUERY_CHARS),
    (build_fts_query, " ".join(f"key{index}" for index in range(MAX_QUERY_TOKENS + 1))),
])
def test_limits_raise_instead_of_silently_truncating_or_searching_everything(function, text) -> None:
    with pytest.raises(FTSTextLimitError):
        function(text)


def test_version_pins_rules_and_unicode_normalization_identity() -> None:
    assert FTS_TEXT_VERSION == "codecrew-fts-text-v1"
    assert FTS_TEXT_REVISION.startswith(FTS_TEXT_VERSION + ":unicode-")
    assert FTS_TOKENIZER == "ascii"


def test_partial_bigram_overlap_is_not_full_phrase_or_semantic_proof(fts) -> None:
    # OR intentionally returns both; later filtering/ranking and answer fidelity
    # must not treat this lexical match as proof of an exact policy or quantity.
    assert set(_search(fts, ["生产禁止兜底", "联调允许兜底"], "生产兜底")) == {1, 2}
