"""SDK-independent parsing; a textual @name never grants group admission."""

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.feishu.models import FeishuInbound


@dataclass(frozen=True)
class ParseResult:
    disposition: str
    inbound: FeishuInbound | None = None


class FeishuAdapter:
    def __init__(self, app_id: str, bot_open_id: str | None) -> None:
        self.app_id = app_id
        self.bot_open_id = bot_open_id

    def parse(self, payload: object) -> ParseResult:
        try:
            return self._parse(payload)
        except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
            # Do not include the SDK payload or Pydantic input values in diagnostics.
            return ParseResult("invalid_event")

    def _parse(self, payload: dict) -> ParseResult:
        header, event = payload["header"], payload["event"]
        if header["event_type"] != "im.message.receive_v1":
            return ParseResult("unsupported_event")
        if header["app_id"] != self.app_id:
            return ParseResult("wrong_application")
        sender, message = event["sender"], event["message"]
        open_id = sender["sender_id"]["open_id"]
        if sender["sender_type"] != "user" or (self.bot_open_id and open_id == self.bot_open_id):
            return ParseResult("self_or_bot_echo")
        if message["message_type"] != "text":
            return ParseResult("unsupported_message")
        content = message["content"]
        if not isinstance(content, str) or len(content) > 100_000:
            return ParseResult("invalid_content")
        text = json.loads(content)["text"]
        if not isinstance(text, str):
            return ParseResult("invalid_content")
        mentions = message.get("mentions") or []
        if not isinstance(mentions, list) or len(mentions) > 100:
            return ParseResult("invalid_mentions")
        placeholders = []
        for mention in mentions:
            if self.bot_open_id and mention.get("id", {}).get("open_id") == self.bot_open_id:
                key = mention["key"]
                # Only real platform-generated placeholders, never @白金 or text names.
                if not isinstance(key, str) or not re.fullmatch(r"@_user_\d+", key):
                    return ParseResult("invalid_mentions")
                placeholders.append(key)
        mentions_bot = bool(placeholders)
        if message["chat_type"] == "group" and not mentions_bot:
            return ParseResult("group_not_mentioned")
        for placeholder in sorted(set(placeholders), key=len, reverse=True):
            text = re.sub(re.escape(placeholder) + r"(?!\w)", "", text)
        inbound = FeishuInbound(
            app_id=header["app_id"], event_id=header["event_id"],
            message_id=message["message_id"], chat_id=message["chat_id"],
            chat_type=message["chat_type"], sender_open_id=open_id,
            text=text.strip(), mentions_bot=mentions_bot,
        )
        return ParseResult("accepted", inbound)
