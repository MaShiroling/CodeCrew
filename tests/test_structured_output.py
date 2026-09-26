import json

import pytest

from app.structured_output import parse_json_response


@pytest.mark.parametrize(
    "wrapper",
    [
        "{}",
        "```json\n{}\n```",
        "已读取 Plan。\n```json\n{}\n```",
        "```json\n{}\n```\n等待回复。",
        "说明\r\n```json\r\n{}\r\n```\r\n结束",
    ],
)
def test_chat_normalizes_only_one_explicit_payload(wrapper):
    raw = json.dumps(
        {"actions": [{"action": "finish_turn", "content": "保留内容"}]}, ensure_ascii=False
    )
    text = wrapper.format(raw)
    assert parse_json_response(text, allow_surrounding_prose=True) == json.loads(raw)
    assert text == wrapper.format(raw)  # Input is not rewritten.


@pytest.mark.parametrize(
    "text",
    [
        "解释\n```json\n{}\n```",
        "```json\n{}\n```\n解释",
    ],
)
def test_legacy_default_does_not_opt_into_prose(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(text)


@pytest.mark.parametrize(
    "text",
    [
        "已批准",
        '说明 {"actions": []}',
        "```json\n{}\n```\n```json\n{}\n```",
        "```python\n{}\n```\n```json\n{}\n```",
        "```\n{}\n```",
        "```JSON\n{}\n```",
        "~~~json\n{}\n~~~",
        "```json\n{}",
        "```json\n{}\n``` trailing",
        "```json\n{}\n```\n```",
        "```json\n{}\n```\n~~~",
        "{}\n```json\n{}\n```",
        "```json\n{}\n```\n[{}]",
        "```json\n{} {}\n```",
        "```json\nnot-json\n```",
        '```json\n{"actions": [], "actions": []}\n```',
        '```json\n{"cost": NaN}\n```',
        "说明" * 8_001 + "\n```json\n{}\n```",
        "[" * 101 + "\n```json\n{}\n```",
    ],
)
def test_chat_rejects_ambiguous_or_invalid_presentation(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(text, allow_surrounding_prose=True)


@pytest.mark.parametrize("text", ['{"a": 1, "a": 2}', '{"a": Infinity}', '{"a": NaN}'])
def test_even_raw_json_must_be_unambiguous_standard_json(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(text, allow_surrounding_prose=True)
