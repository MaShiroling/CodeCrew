"""Versioned, deterministic pre-tokenization for a future memory FTS index.

Not wired into production retrieval yet. Original memory text stays canonical;
only a derived field uses these tokens with FTS5's ``ascii`` tokenizer.
"""

import re
import unicodedata
from dataclasses import dataclass

FTS_TEXT_VERSION = "codecrew-fts-text-v1"
FTS_TEXT_REVISION = f"{FTS_TEXT_VERSION}:unicode-{unicodedata.unidata_version}"
FTS_TOKENIZER = "ascii"
MAX_TEXT_CHARS = 4096
MAX_QUERY_CHARS = 1024
MAX_QUERY_TOKENS = 64

# CJK Extension A, unified ideographs, supplementary extensions. NFKC handles
# compatible/full-width forms before the pattern is applied.
_HAN = r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f\U00030000-\U000323af]+"
_ASCII_ATOM = r"(?:[./]|[+-](?=\d))?[a-z0-9_]+(?:[./:@-][a-z0-9_]+)*(?:\+\+|#)?"
_PART = re.compile(f"(?P<han>{_HAN})|(?P<atom>{_ASCII_ATOM})")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?", re.ASCII)
_WORD = re.compile(r"[a-z0-9]+")
_UNIT = re.compile(r"([+-]?\d+(?:\.\d+)?)(ms|s|kb|mb|gb|tb|hz|khz|mhz|ghz)")
_LEADING_MENTION = re.compile(r"^\s*@\S+(?:\s+|$)")


class FTSTextLimitError(ValueError):
    """Caller should handle explicitly, not silently issue a full-table search."""


@dataclass(frozen=True, slots=True)
class FTSToken:
    kind: str
    value: str

    @property
    def encoded(self) -> str:
        # Reversible, collision-free representation; NOT encryption/redaction.
        # ASCII alphanumeric tokens preserve C++ / C# / decimals as one FTS term
        # and prevent raw text from becoming MATCH operators or column syntax.
        return self.kind + self.value.encode("utf-8").hex()


def _normalize(text: str, *, limit: int) -> str:
    if len(text) > limit:
        raise FTSTextLimitError("FTS input exceeds character limit")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if len(normalized) > limit:
        raise FTSTextLimitError("normalized FTS input exceeds character limit")
    return normalized


def _atom_tokens(atom: str) -> tuple[FTSToken, ...]:
    if _NUMBER.fullmatch(atom):
        # Keep signs, leading zeros and decimal points; do not equate 8 / 80 / 8.5.
        return (FTSToken("n", atom),)
    unit = _UNIT.fullmatch(atom)
    if unit is not None:
        # Numeric aliases support e.g. querying 8080 or 8 across Chinese/ASCII
        # adjacency. They do not establish semantic equivalence of time units.
        return (FTSToken("t", atom), FTSToken("n", unit[1]))
    if _WORD.fullmatch(atom):
        return (FTSToken("w", atom),)
    tokens = [FTSToken("t", atom)]
    # Identifiers/paths/hyphenated names can also match their word components.
    # Dotted versions, decimal-like identifiers, C++ and C# remain atomic.
    if not any(marker in atom for marker in (".", ":", "+", "#", "@")):
        tokens.extend(
            FTSToken("w", part) for part in re.split(r"[_/\-]+", atom)
            if _WORD.fullmatch(part) and re.search("[a-z]", part)
        )
    return tuple(tokens)


def _tokenize_normalized(text: str) -> tuple[FTSToken, ...]:
    tokens = []
    for match in _PART.finditer(text):
        han = match.group("han")
        if han is not None:
            # No dictionary, synonyms or linguistic word segmentation. Two-Han
            # substrings work inside unspaced sentences; single Han runs ignored.
            tokens.extend(FTSToken("h", han[index:index + 2])
                          for index in range(len(han) - 1))
        else:
            tokens.extend(_atom_tokens(match.group("atom")))
    return tuple(tokens)


def tokenize_fts_text(text: str) -> tuple[FTSToken, ...]:
    """Ordered occurrences, including repetitions, for inspection and BM25 TF."""
    return _tokenize_normalized(_normalize(text, limit=MAX_TEXT_CHARS))


def encode_fts_document(text: str) -> str:
    """Derived ASCII field only; never replace canonical content with this."""
    return " ".join(token.encoded for token in tokenize_fts_text(text))


def build_fts_query(text: str) -> str | None:
    """Quoted literal terms OR-ed for candidate retrieval, not user FTS syntax.

    Empty/nonsearchable input returns None; the caller must return no lexical
    candidates, not run MATCH '' or an unfiltered SELECT. Oversize input raises;
    future callers may fall back explicitly, never silently truncate intent.
    """
    normalized = _normalize(text, limit=MAX_QUERY_CHARS)
    while mention := _LEADING_MENTION.match(normalized):
        normalized = normalized[mention.end():]
    tokens = tuple(dict.fromkeys(token.encoded for token in _tokenize_normalized(normalized)))
    if len(tokens) > MAX_QUERY_TOKENS:
        raise FTSTextLimitError("FTS query exceeds unique token limit")
    return " OR ".join(f'"{token}"' for token in tokens) if tokens else None
