"""Normalize JSON presentation without extracting answers from surrounding prose."""

import json
import re
from typing import Any


def parse_json_response(text: str) -> Any:
    """Accept raw JSON or a whole, single explicitly JSON-labelled code block.

    Schema and authorization checks remain the caller's responsibility. Multiple
    blocks, prose, and unlabeled fences are not accepted or searched for JSON.
    """
    fenced = re.fullmatch(r"```json[ \t]*\r?\n(.*?)\r?\n```", text.strip(), re.DOTALL)
    if fenced is not None:
        text = fenced.group(1)
    return json.loads(text)
