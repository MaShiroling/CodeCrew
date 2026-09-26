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
        "已阅读 Plan v1。\n\n{}",
        "说明\r\n  {}\r\n\t",
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
        "说明\n{}",
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
        '说明\n{"actions": []}\n已完成',
        '说明\n{"actions": []}\n{"actions": []}',
        '说明 {"example": true}\n{"actions": []}',
        '说明 [1]\n{"actions": []}',
        '说明\n[broken\n{"actions": []}',
        '说明\n{broken\n{"actions": []}',
        '说明\n[{"actions": []}]',
        '说明\n{"actions": [], "actions": []}',
        '说明\n{"cost": NaN}',
        '说明\n{"cost": Infinity}',
        '说明\n{"actions": []',
        '说明 ```json\n{"actions": []}',
        '说明 ~~~\n{"actions": []}',
        "说明" * 8_001 + '\n{"actions": []}',
        "[" * 101 + '\n{"actions": []}',
    ],
)
def test_chat_rejects_ambiguous_or_invalid_presentation(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(text, allow_surrounding_prose=True)


@pytest.mark.parametrize("text", ['{"a": 1, "a": 2}', '{"a": Infinity}', '{"a": NaN}'])
def test_even_raw_json_must_be_unambiguous_standard_json(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(text, allow_surrounding_prose=True)


def test_trailing_object_may_be_multiline_and_preserves_string_content():
    payload = {"actions": [{"action": "finish_turn", "content": "text {not JSON} [1]"}]}
    text = "已阅读 Plan v1，等待澄清。\n" + json.dumps(payload, indent=2)
    assert parse_json_response(text, allow_surrounding_prose=True) == payload


@pytest.mark.parametrize("wrapper", ["{}", "说明\n{}", "说明\n```json\n{}\n```", "  ```json\n{}\n```  "])
def test_syntax_error_coordinates_refer_to_original_response(wrapper):
    malformed = '{"actions": [{"content": "中文"}, "artifact_content": {}]}'
    text = wrapper.format(malformed)
    with pytest.raises(json.JSONDecodeError) as direct:
        json.loads(malformed)
    with pytest.raises(json.JSONDecodeError) as error:
        parse_json_response(text, allow_surrounding_prose=True)
    expected = text.index(malformed) + direct.value.pos
    assert error.value.doc == text
    assert error.value.msg == direct.value.msg
    assert error.value.pos == expected
    assert error.value.lineno == text[:expected].count("\n") + 1
    assert error.value.colno == expected - text.rfind("\n", 0, expected)


def test_strict_fence_also_preserves_original_coordinates():
    text = ' \n```json\n{"value": }\n```\n'
    with pytest.raises(json.JSONDecodeError) as error:
        parse_json_response(text)
    assert error.value.doc == text
    assert error.value.pos == text.index("}")
