"""Only fixed error codes and hashed platform identifiers may reach logs."""

import hashlib
import json
import logging
import re

logger = logging.getLogger("codecrew.feishu")


def safe_identifier(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def log_event(action: str, **identifiers: object) -> None:
    logger.info(json.dumps({"action": action, **{
        name: safe_identifier(value) for name, value in identifiers.items() if value is not None
    }}, sort_keys=True))


def safe_outbound(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Conservative final filter, not a claim of general semantic DLP.

    Only final persisted chat replies enter here. No stderr/artifact/repository
    content is ever loaded by the bridge. Withhold suspicious replies wholesale.
    """
    sensitive = (
        r"(?i)(?:app[_ -]?secret|access[_ -]?token|api[_ -]?key|authorization|bearer)\s*[:= ]",
        r"(?i)(?:\bsk-[A-Za-z0-9_-]{8,}|-----BEGIN .*PRIVATE KEY)",
        r"(?:[A-Za-z]:[\\/]|\\\\[^\s\\]+\\|(?i:file://)|(?<![\w:/])/[A-Za-z0-9_.~-]+(?:/[^\s]*)?)",
        r"(?i)(?:stderr|traceback \(most recent|hidden[_ /-]?tests?|隐藏测试)",
    )
    if any(secret and secret in text for secret in secrets) or any(
        re.search(pattern, text) for pattern in sensitive
    ):
        return "这条回复包含可能敏感的内容，未自动转发。请在本地 CodeCrew 查看。"
    # Keep one independently deliverable text per Agent message, below API limits.
    raw = text.encode("utf-8")
    if len(raw) > 12_000:
        return raw[:12_000].decode("utf-8", errors="ignore") + "\n[内容已截断，请在本地查看完整回复]"
    return text
