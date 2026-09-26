"""Bounded JSON presentation normalization; authorization stays with callers."""

import json
import re
from typing import Any


def _load_json(text: str) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError("duplicate JSON object key", text, 0)
            result[key] = value
        return result

    def invalid_constant(value):
        raise json.JSONDecodeError(f"non-JSON constant: {value}", text, 0)

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def parse_json_response(text: str, *, allow_surrounding_prose: bool = False) -> Any:
    """Accept raw JSON or one explicitly JSON-labelled code block.

    Only opted-in chat callers allow prose around a single fenced block. Other
    fences and additional object/array candidates are rejected, never selected.
    This function does not mutate the original output or authorize any action.
    """
    try:
        return _load_json(text)
    except json.JSONDecodeError:
        pass
    fenced = re.fullmatch(r"```json[ \t]*\r?\n(.*?)\r?\n```", text.strip(), re.DOTALL)
    if not allow_surrounding_prose:
        return _load_json(fenced.group(1) if fenced is not None else text)

    lines = text.splitlines(keepends=True)
    markers = [
        index for index, line in enumerate(lines) if line.lstrip().startswith(("```", "~~~"))
    ]
    if len(markers) != 2:
        raise json.JSONDecodeError("expected exactly one JSON code block", text, 0)
    start, end = markers
    if lines[start].strip() != "```json" or lines[end].strip() != "```":
        raise json.JSONDecodeError("invalid JSON code block delimiters", text, 0)
    outside = "".join(lines[:start] + lines[end + 1 :])
    # Bound ambiguity scanning; prose is only a presentation wrapper, not a
    # place to carry a second machine payload. No braces are searched for answers.
    if len(outside) > 16_000 or "```" in outside or "~~~" in outside:
        raise json.JSONDecodeError("ambiguous or oversized JSON wrapper", text, 0)
    positions = list(re.finditer(r"[\[{]", outside))
    if len(positions) > 100:
        raise json.JSONDecodeError("too many possible JSON candidates in wrapper", text, 0)
    decoder = json.JSONDecoder()
    for position in positions:
        try:
            decoder.raw_decode(outside, position.start())
        except json.JSONDecodeError:
            continue
        raise json.JSONDecodeError("additional JSON candidate outside code block", text, 0)
    return _load_json("".join(lines[start + 1 : end]))
