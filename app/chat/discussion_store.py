"""Durable, opt-in scheduling state for one-at-a-time standalone chat runs."""

import json
import sqlite3
from datetime import timedelta
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict

from app.chat.discussion_runs import (
    DiscussionNextAction,
    DiscussionReply,
    DiscussionRun,
    DiscussionRunLimits,
    DiscussionRunStatus,
    DiscussionStopReason,
    reserve_discussion_turn,
    transition_discussion_run,
)
from app.chat.models import ChatTurnStatus, StandaloneChatTurn, StoredStandaloneChatMessage
from app.chat.store import StandaloneChatConflictError, StandaloneChatStore
from app.orchestration.models import utc_now
from app.team.models import MemberRole, RoomStatus


class _Invitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: UUID
    role: MemberRole


class DiscussionRunStore:
    """Keeps a FIFO invitation queue and at most one active Agent turn per run.

    Unfinished work is fenced on startup. No persisted invitation is replayed
    automatically after a process restart.
    """

    def __init__(self, chat: StandaloneChatStore) -> None:
        self.chat = chat
        self.database = chat.database

    def create(
        self,
        root: StoredStandaloneChatMessage,
        *,
        opening_role: MemberRole,
        limits: DiscussionRunLimits | None = None,
    ) -> DiscussionRun:
        limits = DiscussionRunLimits.model_validate(
            (limits or DiscussionRunLimits()).model_dump()
        )
        root_id = root.message.message_id
        with self.database.transaction() as connection:
            stored = self.chat._get_message(connection, root_id)
            if stored.message != root.message:
                raise StandaloneChatConflictError("discussion root message changed")
            room = self.chat._get_room(connection, root.message.room_id)
            if room.status is not RoomStatus.ACTIVE:
                raise StandaloneChatConflictError("discussion room is closed")
            human = next(member for member in room.members if member.role is MemberRole.HUMAN)
            opening = next(
                (member for member in room.members if member.role is opening_role),
                None,
            )
            if (
                stored.message.sender_id != human.member_id
                or stored.message.reply_to is not None
                or opening is None
                or stored.message.recipient_ids != (opening.member_id,)
            ):
                raise StandaloneChatConflictError(
                    "discussion requires a fresh Human message addressed to the opening Agent"
                )
            existing = connection.execute(
                "SELECT * FROM standalone_chat_discussion_runs WHERE root_message_id = ?",
                (str(root_id),),
            ).fetchone()
            if existing is not None:
                run = self._run(existing)
                if run.opening_role is opening_role and run.limits == limits:
                    return run
                raise StandaloneChatConflictError("discussion root already has another run")
            # Optional Feishu integration reserves its bound rooms for one active
            # bounded run, including requests originating from the local web UI.
            has_bindings = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feishu_bindings'"
            ).fetchone()
            if has_bindings and connection.execute(
                "SELECT 1 FROM feishu_bindings WHERE room_id=? AND status='active'",
                (str(room.room_id),),
            ).fetchone():
                rows = connection.execute(
                    "SELECT run_json FROM standalone_chat_discussion_runs WHERE room_id=?",
                    (str(room.room_id),),
                ).fetchall()
                if any(DiscussionRun.model_validate_json(row[0]).status in {
                    DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING, DiscussionRunStatus.PAUSED,
                } for row in rows):
                    raise StandaloneChatConflictError("Feishu room already has an active discussion")
            if (
                connection.execute(
                    "SELECT 1 FROM standalone_chat_turns WHERE correlation_id = ? LIMIT 1",
                    (str(root.message.correlation_id),),
                ).fetchone()
                is not None
            ):
                raise StandaloneChatConflictError(
                    "one-shot Agent turns already exist for this discussion"
                )
            now = utc_now()
            run = DiscussionRun(
                run_id=uuid5(root_id, "bounded-discussion-run"),
                room_id=room.room_id,
                root_message_id=root_id,
                correlation_id=root.message.correlation_id,
                opening_role=opening_role,
                limits=limits,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """INSERT INTO standalone_chat_discussion_runs
                (run_id, room_id, root_message_id, correlation_id, run_json,
                 pending_json, active_turn_id) VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    str(run.run_id),
                    str(run.room_id),
                    str(root_id),
                    str(run.correlation_id),
                    run.model_dump_json(),
                    self._pending_json((_Invitation(message_id=root_id, role=opening_role),)),
                ),
            )
            return run

    def get(self, run_id: UUID) -> DiscussionRun:
        with self.database.connect() as connection:
            return self._run(self._row(connection, run_id))

    def list_for_room(self, room_id: UUID) -> tuple[DiscussionRun, ...]:
        with self.database.connect() as connection:
            self.chat._get_room(connection, room_id)
            rows = connection.execute(
                """SELECT * FROM standalone_chat_discussion_runs
                WHERE room_id = ? ORDER BY rowid""",
                (str(room_id),),
            ).fetchall()
            return tuple(self._run(row) for row in rows)

    def has_pending(self, run_id: UUID) -> bool:
        with self.database.connect() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            return (
                run.status is DiscussionRunStatus.RUNNING
                and not run.pause_requested
                and not run.cancel_requested
                and row["active_turn_id"] is None
                and bool(self._pending(row))
            )

    def request_pause(self, run_id: UUID) -> DiscussionRun:
        """Pause now if idle, otherwise finish the active turn before pausing."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is DiscussionRunStatus.PAUSED or run.pause_requested:
                return run
            if run.status not in {DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING}:
                raise StandaloneChatConflictError("discussion cannot be paused in this state")
            if run.cancel_requested:
                raise StandaloneChatConflictError("discussion cancellation is pending")
            now = utc_now()
            if row["active_turn_id"] is None:
                run = transition_discussion_run(
                    run, DiscussionRunStatus.PAUSED,
                    reason=DiscussionStopReason.HUMAN_PAUSED, at=now,
                )
            else:
                run = DiscussionRun.model_validate(run.model_dump() | {
                    "pause_requested": True, "updated_at": now,
                })
            self._save(connection, run, self._pending(row),
                       UUID(row["active_turn_id"]) if row["active_turn_id"] else None)
            return run

    def resume(self, run_id: UUID) -> DiscussionRun:
        """Resume explicitly, keeping the original turn count and wall deadline."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is DiscussionRunStatus.RUNNING and run.pause_requested:
                run = DiscussionRun.model_validate(run.model_dump() | {
                    "pause_requested": False, "updated_at": utc_now(),
                })
                self._save(connection, run, self._pending(row), UUID(row["active_turn_id"]))
                return run
            if run.status is not DiscussionRunStatus.PAUSED:
                raise StandaloneChatConflictError("only a paused discussion can resume")
            now = utc_now()
            reason = None
            if run.agent_turns_used >= run.limits.max_agent_turns:
                reason = DiscussionStopReason.TURN_LIMIT
            elif run.started_at is not None and now - run.started_at >= timedelta(
                seconds=run.limits.max_elapsed_seconds
            ):
                reason = DiscussionStopReason.TIME_LIMIT
            if reason is not None:
                run = transition_discussion_run(
                    run, DiscussionRunStatus.LIMIT_REACHED, reason=reason, at=now,
                )
                self._ack_invites(connection, run.room_id, self._pending(row))
                self._save(connection, run, (), None)
            else:
                run = transition_discussion_run(run, DiscussionRunStatus.RUNNING, at=now)
                self._save(connection, run, self._pending(row), None)
            return run

    def request_cancel(self, run_id: UUID) -> tuple[DiscussionRun, UUID | None]:
        """Cancel idle work atomically; active work waits for CLI cancellation proof."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is DiscussionRunStatus.CANCELLED:
                return run, None
            if run.status not in {
                DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING,
                DiscussionRunStatus.PAUSED,
            }:
                raise StandaloneChatConflictError("discussion cannot be cancelled in this state")
            active = UUID(row["active_turn_id"]) if row["active_turn_id"] else None
            if active is None:
                run = transition_discussion_run(
                    run, DiscussionRunStatus.CANCELLED,
                    reason=DiscussionStopReason.HUMAN_CANCELLED,
                )
                self._ack_invites(connection, run.room_id, self._pending(row))
                self._save(connection, run, (), None)
            elif not run.cancel_requested:
                run = DiscussionRun.model_validate(run.model_dump() | {
                    "pause_requested": False, "cancel_requested": True,
                    "updated_at": utc_now(),
                })
                self._save(connection, run, self._pending(row), active)
            return run, active

    def finish_cancel(
        self, run_id: UUID, turn_id: UUID, *, confirmed: bool,
    ) -> DiscussionRun:
        """Never label an unconfirmed running process as cleanly cancelled."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is not DiscussionRunStatus.RUNNING or row["active_turn_id"] != str(
                turn_id
            ):
                return run
            if not run.cancel_requested:
                raise StandaloneChatConflictError("discussion cancellation was not requested")
            turn = self._active_turn(connection, row, turn_id, run, allow_queued=True)
            now = utc_now()
            connection.execute(
                """UPDATE standalone_chat_turns
                SET status = ?, error = ?, updated_at = ? WHERE turn_id = ?""",
                (
                    (ChatTurnStatus.CANCELLED if confirmed else ChatTurnStatus.INTERRUPTED).value,
                    ("cancelled by Human" if confirmed else "Agent cancellation was not confirmed"),
                    now.isoformat(), str(turn_id),
                ),
            )
            self._ack_delivery(connection, turn.message_id, turn.recipient_id)
            self._ack_invites(connection, run.room_id, self._pending(row))
            self._ack_orphan_response_invites(connection, run, turn_id)
            run = transition_discussion_run(
                run,
                DiscussionRunStatus.CANCELLED if confirmed else DiscussionRunStatus.INTERRUPTED,
                reason=(
                    DiscussionStopReason.HUMAN_CANCELLED if confirmed
                    else DiscussionStopReason.UNCERTAIN_RESULT
                ),
                at=now,
            )
            self._save(connection, run, (), None)
            return run

    def claim_next(self, run_id: UUID) -> StandaloneChatTurn | None:
        """Atomically reserve the next invitation and its turn budget."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status not in {DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING}:
                return None
            if row["active_turn_id"] is not None:
                return None
            pending = self._pending(row)
            if run.cancel_requested:
                run = transition_discussion_run(
                    run, DiscussionRunStatus.CANCELLED,
                    reason=DiscussionStopReason.HUMAN_CANCELLED,
                )
                self._ack_invites(connection, run.room_id, pending)
                self._save(connection, run, (), None)
                return None
            if run.pause_requested:
                run = transition_discussion_run(
                    run, DiscussionRunStatus.PAUSED,
                    reason=DiscussionStopReason.HUMAN_PAUSED,
                )
                self._save(connection, run, pending, None)
                return None
            if not pending:
                return None
            room = self.chat._get_room(connection, run.room_id)
            if room.status is not RoomStatus.ACTIVE:
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.INTERRUPTED,
                    reason=DiscussionStopReason.UNCERTAIN_RESULT,
                )
                self._ack_invites(connection, run.room_id, pending)
                self._save(connection, run, (), None)
                return None
            now = utc_now()
            if run.status is DiscussionRunStatus.CREATED:
                run = transition_discussion_run(run, DiscussionRunStatus.RUNNING, at=now)
            if run.agent_turns_used >= run.limits.max_agent_turns:
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.LIMIT_REACHED,
                    reason=DiscussionStopReason.TURN_LIMIT,
                    at=now,
                )
                self._ack_invites(connection, run.room_id, pending)
                self._save(connection, run, (), None)
                return None
            if now - run.started_at >= timedelta(seconds=run.limits.max_elapsed_seconds):
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.LIMIT_REACHED,
                    reason=DiscussionStopReason.TIME_LIMIT,
                    at=now,
                )
                self._ack_invites(connection, run.room_id, pending)
                self._save(connection, run, (), None)
                return None
            invite = pending[0]
            source = self.chat._get_message(connection, invite.message_id).message
            recipient = next(member for member in room.members if member.role is invite.role)
            if (
                source.room_id != run.room_id
                or source.correlation_id != run.correlation_id
                or recipient.member_id not in source.recipient_ids
            ):
                raise StandaloneChatConflictError("discussion invitation is invalid")
            run = reserve_discussion_turn(run, at=now)
            turn_id = uuid5(run.run_id, f"turn:{run.agent_turns_used}")
            connection.execute(
                """INSERT INTO standalone_chat_turns
                (turn_id, room_id, message_id, recipient_id, correlation_id,
                 status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(turn_id),
                    str(run.room_id),
                    str(invite.message_id),
                    str(recipient.member_id),
                    str(run.correlation_id),
                    ChatTurnStatus.QUEUED.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._save(connection, run, pending[1:], turn_id)
            return self.chat._turn(
                connection.execute(
                    "SELECT * FROM standalone_chat_turns WHERE turn_id = ?",
                    (str(turn_id),),
                ).fetchone()
            )

    def complete_turn(
        self,
        run_id: UUID,
        turn_id: UUID,
        *,
        response: StoredStandaloneChatMessage,
        reply: DiscussionReply,
    ) -> DiscussionRun:
        """Record a confirmed result, then enqueue ordered structured handoffs."""
        reply = DiscussionReply.model_validate(reply.model_dump())
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            turn = self._active_turn(connection, row, turn_id, run)
            if run.cancel_requested:
                raise StandaloneChatConflictError("discussion cancellation is pending")
            room = self.chat._get_room(connection, run.room_id)
            speaker = next(
                member for member in room.members if member.member_id == turn.recipient_id
            )
            reply.validate_for_speaker(speaker.role)
            saved = self.chat._get_message(connection, response.message.message_id)
            human = next(member for member in room.members if member.role is MemberRole.HUMAN)
            next_members = tuple(
                next(member for member in room.members if member.role is role)
                for role in reply.handoff_to
            )
            expected_recipients = (human.member_id, *(m.member_id for m in next_members))
            message = saved.message
            if (
                saved != response
                or message.room_id != run.room_id
                or message.correlation_id != run.correlation_id
                or message.sender_id != speaker.member_id
                or message.reply_to != turn.message_id
                or message.causation_id != turn.message_id
                or message.recipient_ids != expected_recipients
                or message.content != reply.content
                or message.idempotency_key != f"bounded-run:{run.run_id}:turn:{turn_id}"
            ):
                raise StandaloneChatConflictError("discussion response does not match its turn")
            now = utc_now()
            if now - run.started_at >= timedelta(seconds=run.limits.max_elapsed_seconds):
                raise TimeoutError("discussion deadline elapsed before reply acceptance")
            connection.execute(
                """UPDATE standalone_chat_turns SET status = ?, updated_at = ?
                WHERE turn_id = ? AND status = ?""",
                (
                    ChatTurnStatus.SUCCEEDED.value,
                    now.isoformat(),
                    str(turn_id),
                    ChatTurnStatus.RUNNING.value,
                ),
            )
            connection.execute(
                """UPDATE standalone_chat_deliveries
                SET status = 'acknowledged', acknowledged_at = ?
                WHERE message_id = ? AND recipient_id = ? AND status = 'pending'""",
                (now.isoformat(), str(turn.message_id), str(speaker.member_id)),
            )
            pending = self._pending(row)
            if reply.next_action is DiscussionNextAction.FINISH:
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.FINISHED,
                    reason=DiscussionStopReason.AGENT_FINISHED,
                    at=now,
                )
                pending = ()
            elif reply.next_action is DiscussionNextAction.AWAIT_HUMAN:
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.AWAITING_HUMAN,
                    reason=DiscussionStopReason.HUMAN_INPUT_NEEDED,
                    at=now,
                )
                pending = ()
            else:
                pending = (
                    *pending,
                    *(
                        _Invitation(message_id=message.message_id, role=role)
                        for role in reply.handoff_to
                    ),
                )
                if run.agent_turns_used >= run.limits.max_agent_turns:
                    run = transition_discussion_run(
                        run,
                        DiscussionRunStatus.LIMIT_REACHED,
                        reason=DiscussionStopReason.TURN_LIMIT,
                        at=now,
                    )
                    pending = ()
                elif now - run.started_at >= timedelta(seconds=run.limits.max_elapsed_seconds):
                    run = transition_discussion_run(
                        run,
                        DiscussionRunStatus.LIMIT_REACHED,
                        reason=DiscussionStopReason.TIME_LIMIT,
                        at=now,
                    )
                    pending = ()
                elif run.pause_requested:
                    run = transition_discussion_run(
                        run, DiscussionRunStatus.PAUSED,
                        reason=DiscussionStopReason.HUMAN_PAUSED,
                        at=now,
                    )
            if run.status not in {DiscussionRunStatus.RUNNING, DiscussionRunStatus.PAUSED}:
                self._ack_invites(connection, run.room_id, self._pending(row))
                self._ack_invites(
                    connection,
                    run.room_id,
                    tuple(
                        _Invitation(message_id=message.message_id, role=role)
                        for role in reply.handoff_to
                    ),
                )
            self._save(connection, run, pending, None)
            return run

    def abort_turn(
        self,
        run_id: UUID,
        turn_id: UUID,
        *,
        uncertain: bool,
        error: str,
    ) -> DiscussionRun:
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is not DiscussionRunStatus.RUNNING or row["active_turn_id"] != str(
                turn_id
            ):
                return run
            turn = self._active_turn(
                connection,
                row,
                turn_id,
                run,
                allow_queued=True,
            )
            now = utc_now()
            terminal = ChatTurnStatus.INTERRUPTED if uncertain else ChatTurnStatus.FAILED
            connection.execute(
                """UPDATE standalone_chat_turns
                SET status = ?, error = ?, updated_at = ? WHERE turn_id = ?""",
                (terminal.value, error[:500], now.isoformat(), str(turn.turn_id)),
            )
            self._ack_delivery(connection, turn.message_id, turn.recipient_id)
            self._ack_invites(connection, run.room_id, self._pending(row))
            self._ack_orphan_response_invites(connection, run, turn_id)
            run = transition_discussion_run(
                run,
                DiscussionRunStatus.INTERRUPTED if uncertain else DiscussionRunStatus.FAILED,
                reason=(
                    DiscussionStopReason.UNCERTAIN_RESULT
                    if uncertain
                    else DiscussionStopReason.AGENT_FAILED
                ),
                at=now,
            )
            self._save(connection, run, (), None)
            return run

    def expire_turn(
        self,
        run_id: UUID,
        turn_id: UUID,
        *,
        result_confirmed: bool,
        error: str,
    ) -> DiscussionRun:
        """Stop an active turn at the wall deadline without accepting a late reply.

        An unconfirmed CLI cancellation remains interrupted, not a clean limit stop.
        """
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status is not DiscussionRunStatus.RUNNING or row["active_turn_id"] != str(
                turn_id
            ):
                return run
            turn = self._active_turn(connection, row, turn_id, run, allow_queued=True)
            now = utc_now()
            if now - run.started_at < timedelta(seconds=run.limits.max_elapsed_seconds):
                raise ValueError("discussion deadline has not elapsed")
            terminal = (
                ChatTurnStatus.BUDGET_EXHAUSTED
                if result_confirmed
                else ChatTurnStatus.INTERRUPTED
            )
            connection.execute(
                """UPDATE standalone_chat_turns
                SET status = ?, error = ?, updated_at = ? WHERE turn_id = ?""",
                (terminal.value, error[:500], now.isoformat(), str(turn_id)),
            )
            self._ack_delivery(connection, turn.message_id, turn.recipient_id)
            self._ack_invites(connection, run.room_id, self._pending(row))
            self._ack_orphan_response_invites(connection, run, turn_id)
            run = transition_discussion_run(
                run,
                (
                    DiscussionRunStatus.LIMIT_REACHED
                    if result_confirmed
                    else DiscussionRunStatus.INTERRUPTED
                ),
                reason=(
                    DiscussionStopReason.TIME_LIMIT
                    if result_confirmed
                    else DiscussionStopReason.UNCERTAIN_RESULT
                ),
                at=now,
            )
            self._save(connection, run, (), None)
            return run

    def interrupt_run(self, run_id: UUID, *, error: str) -> DiscussionRun:
        """Fence a scheduler failure even when no Agent turn was claimed."""
        with self.database.transaction() as connection:
            row = self._row(connection, run_id)
            run = self._run(row)
            if run.status not in {DiscussionRunStatus.CREATED, DiscussionRunStatus.RUNNING}:
                return run
            now = utc_now()
            if row["active_turn_id"] is not None:
                turn_id = UUID(row["active_turn_id"])
                turn = self._active_turn(connection, row, turn_id, run, allow_queued=True)
                connection.execute(
                    """UPDATE standalone_chat_turns
                    SET status = ?, error = ?, updated_at = ? WHERE turn_id = ?""",
                    (ChatTurnStatus.INTERRUPTED.value, error[:500], now.isoformat(), str(turn_id)),
                )
                self._ack_delivery(connection, turn.message_id, turn.recipient_id)
                self._ack_orphan_response_invites(connection, run, turn_id)
            self._ack_invites(connection, run.room_id, self._pending(row))
            run = transition_discussion_run(
                run,
                DiscussionRunStatus.INTERRUPTED,
                reason=DiscussionStopReason.UNCERTAIN_RESULT,
                at=now,
            )
            self._save(connection, run, (), None)
            return run

    def interrupt_unfinished(self) -> int:
        """Fence persisted work from a previous process; never resume it silently."""
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM standalone_chat_discussion_runs",
            ).fetchall()
            count = 0
            for row in rows:
                run = self._run(row)
                if run.status not in {
                    DiscussionRunStatus.CREATED,
                    DiscussionRunStatus.RUNNING,
                }:
                    continue
                now = utc_now()
                run = transition_discussion_run(
                    run,
                    DiscussionRunStatus.INTERRUPTED,
                    reason=DiscussionStopReason.SERVER_RESTART,
                    at=now,
                )
                if row["active_turn_id"] is not None:
                    turn_row = connection.execute(
                        "SELECT message_id, recipient_id FROM standalone_chat_turns WHERE turn_id = ?",
                        (row["active_turn_id"],),
                    ).fetchone()
                    if turn_row is not None:
                        self._ack_delivery(
                            connection,
                            UUID(turn_row["message_id"]),
                            UUID(turn_row["recipient_id"]),
                        )
                    connection.execute(
                        """UPDATE standalone_chat_turns
                        SET status = 'interrupted',
                        error = 'server restarted before a confirmed discussion result',
                        updated_at = ?
                        WHERE turn_id = ? AND status IN ('queued', 'running')""",
                        (now.isoformat(), row["active_turn_id"]),
                    )
                    self._ack_orphan_response_invites(
                        connection,
                        run,
                        UUID(row["active_turn_id"]),
                    )
                self._ack_invites(connection, run.room_id, self._pending(row))
                self._save(connection, run, (), None)
                count += 1
            return count

    @staticmethod
    def _run(row: sqlite3.Row) -> DiscussionRun:
        return DiscussionRun.model_validate_json(row["run_json"])

    @staticmethod
    def _pending(row: sqlite3.Row) -> tuple[_Invitation, ...]:
        return tuple(_Invitation.model_validate(item) for item in json.loads(row["pending_json"]))

    @staticmethod
    def _pending_json(pending: tuple[_Invitation, ...]) -> str:
        return json.dumps([item.model_dump(mode="json") for item in pending])

    @staticmethod
    def _row(connection: sqlite3.Connection, run_id: UUID) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM standalone_chat_discussion_runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise StandaloneChatConflictError("discussion run not found")
        return row

    def _active_turn(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        turn_id: UUID,
        run: DiscussionRun,
        *,
        allow_queued: bool = False,
    ) -> StandaloneChatTurn:
        if run.status is not DiscussionRunStatus.RUNNING or row["active_turn_id"] != str(turn_id):
            raise StandaloneChatConflictError("discussion turn is not active")
        turn_row = connection.execute(
            "SELECT * FROM standalone_chat_turns WHERE turn_id = ?",
            (str(turn_id),),
        ).fetchone()
        if turn_row is None:
            raise StandaloneChatConflictError("discussion turn is missing")
        turn = self.chat._turn(turn_row)
        allowed = (
            {ChatTurnStatus.RUNNING, ChatTurnStatus.QUEUED}
            if allow_queued
            else {ChatTurnStatus.RUNNING}
        )
        if turn.status not in allowed or turn.correlation_id != run.correlation_id:
            raise StandaloneChatConflictError("discussion turn is not running")
        return turn

    def _save(
        self,
        connection: sqlite3.Connection,
        run: DiscussionRun,
        pending: tuple[_Invitation, ...],
        active_turn_id: UUID | None,
    ) -> None:
        connection.execute(
            """UPDATE standalone_chat_discussion_runs
            SET run_json = ?, pending_json = ?, active_turn_id = ? WHERE run_id = ?""",
            (
                run.model_dump_json(),
                self._pending_json(pending),
                str(active_turn_id) if active_turn_id else None,
                str(run.run_id),
            ),
        )

    @staticmethod
    def _ack_delivery(
        connection: sqlite3.Connection,
        message_id: UUID,
        recipient_id: UUID,
    ) -> None:
        connection.execute(
            """UPDATE standalone_chat_deliveries
            SET status = 'acknowledged', acknowledged_at = ?
            WHERE message_id = ? AND recipient_id = ? AND status = 'pending'""",
            (utc_now().isoformat(), str(message_id), str(recipient_id)),
        )

    def _ack_invites(
        self,
        connection: sqlite3.Connection,
        room_id: UUID,
        pending: tuple[_Invitation, ...],
    ) -> None:
        members = self.chat._get_room(connection, room_id).members
        by_role = {member.role: member.member_id for member in members}
        for invitation in pending:
            self._ack_delivery(
                connection,
                invitation.message_id,
                by_role[invitation.role],
            )

    @staticmethod
    def _ack_orphan_response_invites(
        connection: sqlite3.Connection,
        run: DiscussionRun,
        turn_id: UUID,
    ) -> None:
        """A reply may have been saved just before process death, but not scheduled."""
        connection.execute(
            """UPDATE standalone_chat_deliveries
            SET status = 'acknowledged', acknowledged_at = ?
            WHERE status = 'pending'
            AND message_id IN (
                SELECT message_id FROM standalone_chat_messages
                WHERE room_id = ? AND idempotency_key = ?
            )
            AND recipient_id IN (
                SELECT member_id FROM standalone_chat_members
                WHERE room_id = ? AND role IN ('planner', 'implementer', 'reviewer')
            )""",
            (
                utc_now().isoformat(),
                str(run.room_id),
                f"bounded-run:{run.run_id}:turn:{turn_id}",
                str(run.room_id),
            ),
        )
