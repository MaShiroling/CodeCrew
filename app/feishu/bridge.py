"""Allowlisted, durable entrance to existing read-only bounded chat only."""

import asyncio
from uuid import UUID, uuid5

from app.chat.bounded_dispatch import BoundedDiscussionDispatcher
from app.chat.models import ExternalChatSource
from app.chat.service import ChatInvalid, StandaloneChatService
from app.chat.store import StandaloneChatConflictError, StandaloneChatMessageNotFoundError
from app.config import Settings
from app.feishu.models import FeishuInbound, IngressResult
from app.feishu.privacy import log_event, safe_identifier
from app.feishu.store import FeishuConflict, FeishuStore, platform_key
from app.team.models import MemberRole, RoomStatus

BUSY_NOTICE = "当前讨论进行中，请稍后再发。"
INTERRUPTED_NOTICE = "讨论因服务重启中断，未自动重新调用 Agent。请发送一条新消息。"


class FeishuBridge:
    def __init__(self, service: StandaloneChatService, dispatcher: BoundedDiscussionDispatcher,
                 store: FeishuStore, settings: Settings) -> None:
        if dispatcher.chat is not service.store or store.chat is not service.store:
            raise ValueError("Feishu must share the existing standalone chat store")
        self.service = service
        self.dispatcher = dispatcher
        self.store = store
        self.settings = settings
        self._locks: dict[str, asyncio.Lock] = {}

    def allowed(self, event: FeishuInbound) -> bool:
        return bool(
            self.settings.feishu_enabled and event.app_id == self.store.app_id
            and event.chat_id in self.settings.feishu_allowed_chat_ids
            and event.sender_open_id in self.settings.feishu_allowed_sender_open_ids
            and event.sender_open_id != self.settings.feishu_bot_open_id
            and (event.chat_type == "p2p" or (
                self.settings.feishu_bot_open_id and event.mentions_bot
            ))
        )

    async def receive(self, event: FeishuInbound) -> IngressResult:
        event = FeishuInbound.model_validate(event.model_dump())
        if not self.allowed(event):
            log_event("ingress_denied", event=event.event_id, message=event.message_id)
            return IngressResult(disposition="denied")
        try:
            role = self.service.external_opening_role(event.text)
        except ChatInvalid:
            log_event("unknown_or_empty_agent_request", event=event.event_id)
            return IngressResult(disposition="invalid_route")
        async with self._locks.setdefault(event.chat_id, asyncio.Lock()):
            try:
                result = self._admit(event, role)
            except FeishuConflict:
                log_event("ingress_identity_conflict", event=event.event_id, message=event.message_id)
                return IngressResult(disposition="identity_conflict")
        log_event("ingress_" + result.disposition, event=event.event_id, message=event.message_id,
                  room=result.room_id, run=result.run_id, trace=self.service.get_room(
                      result.room_id).trace_id if result.room_id else None)
        return result

    def _admit(self, event: FeishuInbound, role: MemberRole) -> IngressResult:
        binding = self.store.binding(event.chat_id)
        if binding is None:
            room = self.service.create_room(
                title=f"飞书会话 {safe_identifier(event.chat_id)}",
                idempotency_key=platform_key(event.app_id, "chat", event.chat_id),
            )
            binding = self.store.bind(event.chat_id, room.room_id)
        if binding["status"] != "active":
            return IngressResult(disposition="binding_disabled")
        room_id = UUID(binding["room_id"])
        if self.service.get_room(room_id).status is not RoomStatus.ACTIVE:
            return IngressResult(disposition="room_closed", room_id=room_id)
        ingress = self.store.claim(event, room_id, role.value)
        if ingress["status"] != "received":
            self._ensure_notice(ingress)
            return self._result(ingress)
        # Recover a persisted root/run before checking busy. Replays of this run
        # are not new requests, and must never be mistaken for a busy new ingress.
        root = self._existing_root(ingress)
        if root is not None:
            try:
                run = self.dispatcher.runs.get(uuid5(root.message.message_id, "bounded-discussion-run"))
            except StandaloneChatConflictError:
                run = None
            if run is not None:
                ingress = self.store.finish_ingress(ingress["ingress_id"], "admitted",
                                                    message=root.message, run=run)
                return self._result(ingress)  # Never schedule an existing uncertain run.
        if self.store.busy(room_id, except_ingress=ingress["ingress_id"]):
            ingress = self.store.finish_ingress(ingress["ingress_id"], "busy")
            self._ensure_notice(ingress)
            return self._result(ingress)
        root = self.service.post_external_message(
            room_id, content=event.text,
            idempotency_key=platform_key(event.app_id, "message", event.message_id),
            external_source=ExternalChatSource(
                external_chat_id=event.chat_id, external_sender_id=event.sender_open_id,
                display_name=event.display_name,
            ), opening_role=role,
        )
        # Persist run and ingress BEFORE scheduling. Nothing above invokes a model.
        run = self.dispatcher.runs.create(root, opening_role=role)
        ingress = self.store.finish_ingress(ingress["ingress_id"], "admitted",
                                            message=root.message, run=run)
        self.dispatcher.start(root.message.message_id, opening_role=role)
        return self._result(ingress)

    def _existing_root(self, ingress):
        key = platform_key(self.store.app_id, "message", ingress["message_id"])
        root_id = uuid5(UUID(ingress["room_id"]), f"external-chat:{key}")
        try:
            return self.service.store.get_message(root_id)
        except StandaloneChatMessageNotFoundError:
            return None

    def recover(self) -> None:
        """Called after bounded startup fencing; no persisted work is re-executed."""
        for ingress in self.store.ingresses():
            if ingress["status"] == "received":
                root = self._existing_root(ingress)
                if root is not None:
                    try:
                        run = self.dispatcher.runs.get(uuid5(root.message.message_id, "bounded-discussion-run"))
                    except StandaloneChatConflictError:
                        if self.service.get_room(root.message.room_id).status is not RoomStatus.ACTIVE:
                            ingress = self.store.finish_ingress(ingress["ingress_id"], "interrupted", message=root.message)
                            self._ensure_notice(ingress)
                            continue
                        run = self.dispatcher.runs.create(
                            root, opening_role=MemberRole(ingress["opening_role"]),
                        )
                    run = self.dispatcher.runs.interrupt_run(run.run_id, error="Feishu ingress interrupted")
                    ingress = self.store.finish_ingress(ingress["ingress_id"], "admitted",
                                                        message=root.message, run=run)
                else:
                    ingress = self.store.finish_ingress(ingress["ingress_id"], "interrupted")
            self._ensure_notice(ingress)

    def _ensure_notice(self, ingress) -> None:
        if ingress["status"] == "busy":
            self.store.notice(ingress, BUSY_NOTICE)
        elif ingress["status"] == "interrupted":
            self.store.notice(ingress, INTERRUPTED_NOTICE)

    @staticmethod
    def _result(row) -> IngressResult:
        return IngressResult(
            disposition=row["status"], room_id=row["room_id"],
            message_id=row["human_message_id"], correlation_id=row["correlation_id"],
            run_id=row["run_id"],
        )
