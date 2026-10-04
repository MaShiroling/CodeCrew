"""Scan only admitted external correlations; retry delivery, never inference."""

import asyncio
import re

from app.chat.discussion_runs import DiscussionRun, DiscussionRunStatus
from app.chat.models import StandaloneChatMessage
from app.config import Settings
from app.feishu.privacy import log_event, safe_identifier, safe_outbound
from app.feishu.store import ACTIVE_RUNS, FeishuStore
from app.orchestration.models import utc_now

_NOTICES = {
    DiscussionRunStatus.FINISHED: "本次讨论已结束。",
    DiscussionRunStatus.LIMIT_REACHED: "本次讨论已达到回合或时间上限。",
    DiscussionRunStatus.FAILED: "Agent 回复失败，本次讨论已停止。",
    DiscussionRunStatus.AWAITING_HUMAN: "团队需要你补充信息，请发送一条新消息。",
    DiscussionRunStatus.INTERRUPTED: "讨论已中断（重启或结果未确认），未自动重新调用 Agent。",
    DiscussionRunStatus.CANCELLED: "本次讨论已取消。",
}


class FeishuOutbox:
    def __init__(self, store: FeishuStore, sender, settings: Settings,
                 *, secrets: tuple[str, ...] = ()) -> None:
        self.store, self.sender, self.settings = store, sender, settings
        self.secrets = (*secrets, settings.feishu_app_secret.get_secret_value())
        self.last_error: str | None = None
        self._delivery_lock = asyncio.Lock()

    def scan(self) -> None:
        with self.store.database.transaction() as conn:
            bindings = conn.execute("SELECT * FROM feishu_bindings WHERE app_id=? AND status='active'",
                                    (self.store.app_id,)).fetchall()
            for binding in bindings:
                # Cursor advances across local messages too, but only matching Agent
                # messages can produce rows. Cursor + inserts share this transaction.
                rows = conn.execute("""SELECT m.*,member.role,member.kind,member.name
                    FROM standalone_chat_messages m
                    JOIN standalone_chat_members member ON member.member_id=m.sender_id
                    WHERE m.room_id=? AND m.sequence>? ORDER BY m.sequence LIMIT 1000""",
                                    (binding["room_id"], max(binding["scan_cursor"],
                                                           binding["start_sequence"]))).fetchall()
                cursor = binding["scan_cursor"]
                for row in rows:
                    cursor = row["sequence"]
                    if row["kind"] != "agent":
                        continue
                    ingress = conn.execute("""SELECT * FROM feishu_ingress WHERE app_id=?
                        AND chat_id=? AND room_id=? AND correlation_id=? AND status='admitted'
                        AND run_id IS NOT NULL""",
                                           (self.store.app_id, binding["chat_id"], binding["room_id"],
                                            row["correlation_id"])).fetchone()
                    if ingress is None:
                        continue
                    # Bind to a real bounded turn as well as the ingress. A reply
                    # persisted just before a crash is still deliverable; the
                    # separate run notice honestly reports any interruption.
                    turn = conn.execute("""SELECT t.turn_id FROM standalone_chat_turns t
                        WHERE t.correlation_id=? AND t.recipient_id=?
                        AND ?='bounded-run:' || ? || ':turn:' || t.turn_id""",
                                        (row["correlation_id"], row["sender_id"],
                                         row["idempotency_key"], ingress["run_id"])).fetchone()
                    if turn is None:
                        continue
                    message = StandaloneChatMessage.model_validate_json(row["message_json"])
                    content = re.sub(r"^(?:【" + re.escape(row["name"]) + r"】\s*|"
                                     + re.escape(row["name"]) + r"[：:]\s*)", "", message.content)
                    text = (f"【{row['name']}】 · {safe_identifier(ingress['correlation_id'])}\n"
                            + safe_outbound(content, secrets=self.secrets))
                    self.store.enqueue(conn, ingress, kind="agent", key=row["message_id"],
                                       sequence=row["sequence"], text=text, message_id=row["message_id"])
                    log_event("outbox_enqueued", message=row["message_id"], turn=turn["turn_id"],
                              room=binding["room_id"], run=ingress["run_id"])
                conn.execute("UPDATE feishu_bindings SET scan_cursor=?,updated_at=? WHERE app_id=? AND chat_id=?",
                             (cursor, utc_now().isoformat(), self.store.app_id, binding["chat_id"]))
                self._statuses(conn, binding, cursor)

    def _statuses(self, conn, binding, cursor: int) -> None:
        ingresses = conn.execute("""SELECT i.*,r.run_json FROM feishu_ingress i
            JOIN standalone_chat_discussion_runs r ON r.run_id=i.run_id
            WHERE i.app_id=? AND i.chat_id=? AND i.status='admitted'""",
                                (self.store.app_id, binding["chat_id"])).fetchall()
        for ingress in ingresses:
            run = DiscussionRun.model_validate_json(ingress["run_json"])
            if run.status in ACTIVE_RUNS:
                continue
            last_sequence = conn.execute("SELECT MAX(sequence) FROM standalone_chat_messages WHERE correlation_id=?",
                                         (ingress["correlation_id"],)).fetchone()[0]
            if last_sequence is None or cursor < last_sequence:
                continue
            text = f"【CodeCrew】 · {safe_identifier(run.correlation_id)}\n{_NOTICES[run.status]}"
            self.store.enqueue(conn, ingress, kind="status", key=str(run.run_id),
                               sequence=last_sequence, text=text)

    async def deliver_one(self) -> bool:
        async with self._delivery_lock:
            row = self.store.claim_delivery(max_attempts=self.settings.feishu_max_outbox_attempts)
            if row is None:
                return False
            # Recheck current allowlists after restart/config changes as well.
            with self.store.database.connect() as conn:
                sender = conn.execute("SELECT sender_open_id FROM feishu_ingress WHERE ingress_id=?",
                                      (row["ingress_id"],)).fetchone()[0]
                binding = conn.execute("SELECT status FROM feishu_bindings WHERE app_id=? AND chat_id=?",
                                       (self.store.app_id, row["chat_id"])).fetchone()
            if (row["chat_id"] not in self.settings.feishu_allowed_chat_ids
                    or sender not in self.settings.feishu_allowed_sender_open_ids
                    or binding is None or binding[0] != "active"):
                self.store.failed_attempt(row, max_attempts=1, delay=0, error="allowlist_changed")
                self.last_error = "allowlist_changed"
                return True
            try:
                receipt = await self.sender.send_text(
                    row["chat_id"], row["text"], reply_to_message_id=row["reply_to_message_id"],
                )
                if not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", receipt):
                    raise ValueError("invalid_receipt")
            except Exception:  # noqa: BLE001 - never retain provider exceptions/raw response
                self.last_error = "send_failed"
                delay = min(self.settings.feishu_retry_cap_seconds,
                            self.settings.feishu_retry_base_seconds * 2 ** (row["attempt_count"] - 1))
                self.store.failed_attempt(row, max_attempts=self.settings.feishu_max_outbox_attempts,
                                          delay=delay)
                log_event("delivery_retry_or_failed", outbox=row["outbox_id"])
            else:
                self.store.sent(row["outbox_id"], receipt)
                log_event("delivery_sent", outbox=row["outbox_id"], receipt=receipt)
            return True
