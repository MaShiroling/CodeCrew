"""Single-use Human chat-to-code authorization with a durable fail-closed claim."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from app.api.models import CreateTaskRequest
from app.api.service import TaskService
from app.chat.coding_intent import (
    AuthorizeChatCodingTaskRequest,
    AuthorizedCodingTask,
    CodingTaskDraft,
    CodingTaskPreflight,
    preflight_coding_task,
)
from app.chat.service import ChatApiError, ChatConflict, ChatMessageNotFound, StandaloneChatService
from app.chat.store import StandaloneChatMessageNotFoundError
from app.orchestration.models import utc_now
from app.storage import Migration, SQLiteDatabase
from app.workspace import PermissionPolicy


class ChatCodingUnavailable(ChatApiError):
    code = "chat_coding_unavailable"
    status_code = 503


_MIGRATION = Migration(
    version=17,
    name="create_chat_coding_authorizations",
    statements=(
        """
        CREATE TABLE chat_coding_authorizations (
            idempotency_key TEXT PRIMARY KEY,
            room_id TEXT NOT NULL REFERENCES standalone_chat_rooms(room_id),
            source_message_id TEXT NOT NULL REFERENCES standalone_chat_messages(message_id),
            request_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'created')),
            task_id TEXT,
            task_trace_id TEXT,
            repository_path TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            allowed_paths_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(room_id, source_message_id)
        )
        """,
    ),
)


class ChatCodingAuthorizationService:
    """One configured task runtime and one chat database; no implicit chat grant."""

    def __init__(
        self, chat: StandaloneChatService, tasks: TaskService,
        policy: PermissionPolicy,
        *, repository_bound: Path | None = None, issue_bound: str | None = None,
    ) -> None:
        self.chat = chat
        self.tasks = tasks
        self.policy = policy
        self.repository_bound = repository_bound.resolve() if repository_bound else None
        self.issue_bound = issue_bound
        self.database: SQLiteDatabase = chat.store.database
        self.database.initialize((_MIGRATION,))

    def preflight(self, room_id: UUID, draft: CodingTaskDraft) -> CodingTaskPreflight:
        if self.issue_bound is not None and draft.issue != self.issue_bound:
            raise ChatConflict("demo coding only supports its displayed fixed task")
        if (self.repository_bound is not None
                and Path(draft.repository_path).resolve() != self.repository_bound):
            raise ChatConflict("demo coding is limited to its generated example repository")
        return preflight_coding_task(self.chat, room_id, draft)

    async def authorize(
        self, room_id: UUID, command: AuthorizeChatCodingTaskRequest,
    ) -> AuthorizedCodingTask:
        # Check provenance even before returning a previously persisted receipt.
        try:
            source = self.chat.store.get_message(command.source_message_id).message
        except StandaloneChatMessageNotFoundError:
            raise ChatMessageNotFound("coding source message is not in this room") from None
        if source.external_source is not None:
            raise ChatConflict("external Feishu messages cannot authorize coding")
        fingerprint = hashlib.sha256(json.dumps(
            {"room_id": str(room_id), **command.model_dump(mode="json")},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM chat_coding_authorizations WHERE idempotency_key = ?",
                (str(command.idempotency_key),),
            ).fetchone()
        if existing is not None:
            return self._replay(existing, fingerprint)

        # The task workflow uses a single server-wide PermissionGate and Kimi policy.
        # Until it supports per-task policies, require exact equality, never pretend
        # that a narrower Human selection constrains an otherwise broader runtime.
        if set(command.allowed_paths) != set(self.policy.allowed_paths):
            raise ChatConflict("selected write scope must equal the configured task policy")
        preview = self.preflight(room_id, command)
        if preview.base_commit != command.expected_base_commit:
            raise ChatConflict("Git baseline changed; run preflight again")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM chat_coding_authorizations WHERE idempotency_key = ?",
                (str(command.idempotency_key),),
            ).fetchone()
            if existing is not None:
                return self._replay(existing, fingerprint)
            occupied = connection.execute(
                "SELECT idempotency_key FROM chat_coding_authorizations "
                "WHERE room_id = ? AND source_message_id = ?",
                (str(room_id), str(command.source_message_id)),
            ).fetchone()
            if occupied is not None:
                raise ChatConflict("this Human message already has a coding authorization")
            connection.execute(
                """INSERT INTO chat_coding_authorizations
                (idempotency_key, room_id, source_message_id, request_fingerprint,
                 status, repository_path, base_commit, allowed_paths_json, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (str(command.idempotency_key), str(room_id), str(command.source_message_id),
                 fingerprint, preview.repository_path, preview.base_commit,
                 json.dumps(preview.allowed_paths), utc_now().isoformat()),
            )
        # A crash or failed task bootstrap leaves 'pending' and blocks re-dispatch.
        # This deliberately favors no duplicate execution over automatic recovery.
        task = await self.tasks.create_task(CreateTaskRequest(
            issue=preview.issue, repository_path=preview.repository_path,
            expected_base_commit=preview.base_commit,
        ))
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE chat_coding_authorizations
                SET status = 'created', task_id = ?, task_trace_id = ?
                WHERE idempotency_key = ? AND status = 'pending'""",
                (str(task.task_id), str(task.trace_id), str(command.idempotency_key)),
            )
            row = connection.execute(
                "SELECT * FROM chat_coding_authorizations WHERE idempotency_key = ?",
                (str(command.idempotency_key),),
            ).fetchone()
        return self._replay(row, fingerprint)

    @staticmethod
    def _replay(row, fingerprint: str) -> AuthorizedCodingTask:
        if row["request_fingerprint"] != fingerprint:
            raise ChatConflict("authorization key was used for different content")
        if row["status"] != "created":
            raise ChatConflict("authorization is pending; inspect the task before retrying")
        return AuthorizedCodingTask(
            room_id=UUID(row["room_id"]), source_message_id=UUID(row["source_message_id"]),
            idempotency_key=UUID(row["idempotency_key"]), task_id=UUID(row["task_id"]),
            task_trace_id=UUID(row["task_trace_id"]),
            repository_path=row["repository_path"], base_commit=row["base_commit"],
            allowed_paths=tuple(json.loads(row["allowed_paths_json"])),
        )
