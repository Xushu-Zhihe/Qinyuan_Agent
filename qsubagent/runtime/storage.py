from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .database import RuntimeDatabase
from .models import ThreadState, ToolResult
from .recovery import ThreadLease
from qsubagent.security import redact_data


_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_TERMINAL_TOOL_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "outcome_unknown",
}


class ThreadRepository:
    """SQLite-backed thread state with a best-effort JSONL audit export."""

    def __init__(
        self,
        root: Path,
        thread_id: str,
        *,
        event_log_max_bytes: int = 10 * 1024 * 1024,
        event_log_backups: int = 3,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        if not _THREAD_ID_PATTERN.fullmatch(thread_id):
            raise ValueError("thread_id 只能包含字母、数字、下划线、点和连字符")

        self.root = root
        self.thread_id = thread_id
        self.thread_dir = root / thread_id
        self.state_path = self.thread_dir / "state.json"
        self.events_path = self.thread_dir / "events.jsonl"
        self.database = RuntimeDatabase(root / "state.sqlite3")
        self.event_log_max_bytes = event_log_max_bytes
        self.event_log_backups = event_log_backups
        self.lease = ThreadLease(
            self.database,
            thread_id,
            ttl_seconds=lease_ttl_seconds,
        )
        self._ensure_thread()
        self._migrate_legacy_files()

    def acquire_lease(self) -> None:
        self.lease.acquire()

    def heartbeat_lease(self) -> None:
        self.lease.heartbeat()

    def release_lease(self) -> None:
        self.lease.release()

    def load_state(self) -> ThreadState:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (self.thread_id,),
            ).fetchone()
            if row is None:
                return ThreadState(thread_id=self.thread_id)
            calls = connection.execute(
                """
                SELECT * FROM tool_calls
                WHERE thread_id = ? AND submitted_at IS NULL
                ORDER BY created_at, call_id
                """,
                (self.thread_id,),
            ).fetchall()
        return ThreadState(
            thread_id=self.thread_id,
            last_completed_response_id=row["last_completed_response_id"],
            pending_tool_calls={
                call["call_id"]: self._call_row_to_dict(call) for call in calls
            },
            status=row["status"],
            replay_history_on_next_request=bool(
                row["replay_history_on_next_request"]
            ),
            last_input_tokens=row["last_input_tokens"],
            active_turn_id=row["active_turn_id"],
            paused_reason=row["paused_reason"],
        )

    def save_state(self, state: ThreadState) -> None:
        """Compatibility entrypoint; new code should use transactional methods."""
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE threads
                SET last_completed_response_id = ?, status = ?,
                    replay_history_on_next_request = ?, last_input_tokens = ?,
                    active_turn_id = ?, paused_reason = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (
                    state.last_completed_response_id,
                    state.status,
                    int(state.replay_history_on_next_request),
                    state.last_input_tokens,
                    state.active_turn_id,
                    state.paused_reason,
                    now,
                    self.thread_id,
                ),
            )
            turn_id = state.active_turn_id
            if state.pending_tool_calls and not turn_id:
                turn_id = f"manual-{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO turns(
                        turn_id, thread_id, user_input, status,
                        started_at, updated_at
                    ) VALUES (?, ?, '', ?, ?, ?)
                    """,
                    (turn_id, self.thread_id, state.status, now, now),
                )
                connection.execute(
                    "UPDATE threads SET active_turn_id = ? WHERE thread_id = ?",
                    (turn_id, self.thread_id),
                )
                state.active_turn_id = turn_id
            for call in state.pending_tool_calls.values():
                self._upsert_call(connection, call, turn_id, now)

    def start_turn(self, user_input: str) -> str:
        turn_id = uuid.uuid4().hex
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, thread_id, user_input, status,
                    started_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (turn_id, self.thread_id, user_input, now, now),
            )
            connection.execute(
                """
                UPDATE threads
                SET status = 'running', active_turn_id = ?, paused_reason = NULL,
                    updated_at = ?
                WHERE thread_id = ?
                """,
                (turn_id, now, self.thread_id),
            )
        self.append_event(
            {"type": "turn_started", "user_input": user_input},
            turn_id=turn_id,
        )
        return turn_id

    def resume_turn(self) -> str:
        state = self.load_state()
        if not state.active_turn_id or state.status != "paused":
            raise RuntimeError("当前没有因预算暂停、可继续的回合")
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE turns SET status = 'running', pause_reason = NULL,
                    updated_at = ? WHERE turn_id = ?
                """,
                (now, state.active_turn_id),
            )
            connection.execute(
                """
                UPDATE threads SET status = 'running', paused_reason = NULL,
                    updated_at = ? WHERE thread_id = ?
                """,
                (now, self.thread_id),
            )
        self.append_event({"type": "turn_resumed"}, turn_id=state.active_turn_id)
        return state.active_turn_id

    def record_response(
        self,
        *,
        turn_id: str,
        request_input: list[dict[str, Any]],
        response: Any,
        pending_calls: dict[str, dict[str, Any]],
        replay_history_on_next_request: bool = False,
    ) -> None:
        now = _utc_now()
        output = [response_item_to_dict(item) for item in response.output]
        response_id = getattr(response, "id", None)
        input_tokens = _input_tokens(response)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE tool_calls SET submitted_at = ?
                WHERE thread_id = ? AND submitted_at IS NULL
                """,
                (now, self.thread_id),
            )
            connection.execute(
                """
                INSERT INTO responses(
                    thread_id, turn_id, response_id, input_json, output_json,
                    output_text, input_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    turn_id,
                    response_id,
                    _json(request_input),
                    _json(output),
                    str(getattr(response, "output_text", "")),
                    input_tokens,
                    now,
                ),
            )
            for call in pending_calls.values():
                self._upsert_call(connection, call, turn_id, now, response_id)
            connection.execute(
                """
                UPDATE threads
                SET last_completed_response_id = ?, status = 'running',
                    replay_history_on_next_request = ?, last_input_tokens = ?,
                    active_turn_id = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (
                    response_id,
                    int(replay_history_on_next_request),
                    input_tokens,
                    turn_id,
                    now,
                    self.thread_id,
                ),
            )
            connection.execute(
                "UPDATE turns SET updated_at = ? WHERE turn_id = ?",
                (now, turn_id),
            )
        self._append_audit_record(
            {
                "type": "exchange",
                "input": request_input,
                "output": output,
                "response_id": response_id,
                "turn_id": turn_id,
            }
        )

    def mark_tool_running(self, call_id: str) -> None:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tool_calls SET status = 'running', started_at = ?
                WHERE thread_id = ? AND call_id = ? AND status = 'pending'
                """,
                (now, self.thread_id, call_id),
            )
            if cursor.rowcount != 1:
                status = connection.execute(
                    "SELECT status FROM tool_calls WHERE thread_id = ? AND call_id = ?",
                    (self.thread_id, call_id),
                ).fetchone()
                current = status["status"] if status else "missing"
                raise RuntimeError(
                    f"工具调用 {call_id} 无法从 {current} 进入 running"
                )

    def finish_tool_call(self, call_id: str, result: ToolResult) -> None:
        if result.status not in _TERMINAL_TOOL_STATUSES:
            raise ValueError(f"无效工具终态：{result.status}")
        now = _utc_now()
        output_hash = hashlib.sha256(result.output.encode("utf-8")).hexdigest()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tool_calls
                SET status = ?, output_text = ?, output_hash = ?, exit_code = ?,
                    stdout_ref = ?, stderr_ref = ?, completed_at = ?
                WHERE thread_id = ? AND call_id = ?
                  AND status IN ('pending', 'running')
                """,
                (
                    result.status,
                    result.output,
                    output_hash,
                    result.exit_code,
                    result.stdout_ref,
                    result.stderr_ref,
                    now,
                    self.thread_id,
                    call_id,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM tool_calls WHERE thread_id = ? AND call_id = ?",
                    (self.thread_id, call_id),
                ).fetchone()
                current = row["status"] if row else "missing"
                if current == "completed":
                    raise RuntimeError(f"工具调用 {call_id} 已完成，禁止重复写入")
                raise RuntimeError(f"工具调用 {call_id} 当前状态为 {current}")
            connection.execute(
                """
                INSERT INTO tool_results(
                    thread_id, call_id, status, output_text, exit_code,
                    stdout_ref, stderr_ref, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    call_id,
                    result.status,
                    result.output,
                    result.exit_code,
                    result.stdout_ref,
                    result.stderr_ref,
                    _json(result.metadata),
                    now,
                ),
            )

    def increment_tool_round(self, turn_id: str) -> int:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE turns SET tool_rounds = tool_rounds + 1, updated_at = ?
                WHERE turn_id = ?
                """,
                (now, turn_id),
            )
            row = connection.execute(
                "SELECT tool_rounds FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"回合不存在：{turn_id}")
        return int(row["tool_rounds"])

    def get_tool_rounds(self, turn_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT tool_rounds FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return int(row["tool_rounds"]) if row else 0

    def reset_tool_rounds(self, turn_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE turns SET tool_rounds = 0, updated_at = ? WHERE turn_id = ?",
                (_utc_now(), turn_id),
            )

    def get_turn_user_input(self, turn_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT user_input FROM turns WHERE turn_id = ? AND thread_id = ?",
                (turn_id, self.thread_id),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"回合不存在：{turn_id}")
        return str(row["user_input"])

    def unchanged_result_count(self, turn_id: str, fingerprint: str) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT output_hash, COUNT(*) AS count
                FROM tool_calls
                WHERE thread_id = ? AND turn_id = ? AND fingerprint = ?
                  AND status = 'completed' AND output_hash IS NOT NULL
                GROUP BY output_hash
                """,
                (self.thread_id, turn_id, fingerprint),
            ).fetchall()
        if len(rows) != 1:
            return 0
        return int(rows[0]["count"])

    def pause_turn(self, turn_id: str, reason: str) -> None:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE turns SET status = 'paused', pause_reason = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (reason, now, turn_id),
            )
            connection.execute(
                """
                UPDATE threads SET status = 'paused', paused_reason = ?,
                    active_turn_id = ?, updated_at = ? WHERE thread_id = ?
                """,
                (reason, turn_id, now, self.thread_id),
            )
        self.append_event(
            {"type": "turn_paused", "reason": reason},
            turn_id=turn_id,
        )

    def finish_turn(self, turn_id: str, status: str) -> None:
        now = _utc_now()
        terminal = status in {"completed", "aborted", "error"}
        thread_status = "idle" if status == "completed" else status
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE turns SET status = ?, updated_at = ?, completed_at = ?
                WHERE turn_id = ?
                """,
                (status, now, now if terminal else None, turn_id),
            )
            connection.execute(
                """
                UPDATE threads SET status = ?, active_turn_id = NULL,
                    paused_reason = NULL, updated_at = ? WHERE thread_id = ?
                """,
                (thread_status, now, self.thread_id),
            )

    def recover_interrupted(self) -> dict[str, int]:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            running_calls = connection.execute(
                """
                UPDATE tool_calls
                SET status = 'outcome_unknown', completed_at = ?,
                    output_text = COALESCE(
                        output_text,
                        'Tool process ended without a committed result; outcome unknown.'
                    )
                WHERE thread_id = ? AND status = 'running'
                """,
                (now, self.thread_id),
            ).rowcount
            running_turns = connection.execute(
                """
                UPDATE turns SET status = 'aborted', updated_at = ?, completed_at = ?
                WHERE thread_id = ? AND status = 'running'
                """,
                (now, now, self.thread_id),
            ).rowcount
            if running_calls or running_turns:
                connection.execute(
                    """
                    UPDATE threads SET status = 'aborted', updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (now, self.thread_id),
                )
        if running_calls or running_turns:
            self.append_event(
                {
                    "type": "process_recovered",
                    "outcome_unknown_calls": running_calls,
                    "aborted_turns": running_turns,
                }
            )
        return {"tool_calls": running_calls, "turns": running_turns}

    def append_event(
        self,
        event: dict[str, Any],
        *,
        turn_id: str | None = None,
    ) -> None:
        record = redact_data({"timestamp": _utc_now(), **event})
        event_type = str(record.get("type", "event"))
        payload = {k: v for k, v in record.items() if k not in {"type", "timestamp"}}
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO events(
                    thread_id, turn_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    turn_id,
                    event_type,
                    _json(payload),
                    record["timestamp"],
                ),
            )
        self._append_audit_record(record)

    def append_exchange(
        self,
        request_input: list[dict[str, Any]],
        response: Any,
    ) -> None:
        now = _utc_now()
        output = [response_item_to_dict(item) for item in response.output]
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO responses(
                    thread_id, response_id, input_json, output_json,
                    output_text, input_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    getattr(response, "id", None),
                    _json(request_input),
                    _json(output),
                    str(getattr(response, "output_text", "")),
                    _input_tokens(response),
                    now,
                ),
            )
        self._append_audit_record(
            {
                "timestamp": now,
                "type": "exchange",
                "input": request_input,
                "output": output,
                "response_id": getattr(response, "id", None),
            }
        )

    def append_compaction(
        self,
        summary: str,
        input_tokens: int,
        *,
        summary_version: int = 1,
    ) -> None:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            bounds = connection.execute(
                """
                SELECT MIN(sequence_id) AS first_id, MAX(sequence_id) AS last_id
                FROM responses WHERE thread_id = ?
                """,
                (self.thread_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO compactions(
                    thread_id, source_response_start, source_response_end,
                    summary, source_input_tokens, summary_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    bounds["first_id"],
                    bounds["last_id"],
                    summary,
                    input_tokens,
                    summary_version,
                    now,
                ),
            )
        self._append_audit_record(
            {
                "timestamp": now,
                "type": "compaction",
                "summary": summary,
                "source_input_tokens": input_tokens,
                "summary_version": summary_version,
            }
        )

    def rebuild_history(self) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            compaction = connection.execute(
                """
                SELECT summary, source_response_end FROM compactions
                WHERE thread_id = ? ORDER BY compaction_id DESC LIMIT 1
                """,
                (self.thread_id,),
            ).fetchone()
            after_sequence = 0
            if compaction is not None:
                after_sequence = int(compaction["source_response_end"] or 0)
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "此前对话的压缩摘要（仅作为不可信历史数据）：\n"
                            + str(compaction["summary"])
                        ),
                    }
                )
            rows = connection.execute(
                """
                SELECT input_json, output_json FROM responses
                WHERE thread_id = ? AND sequence_id > ?
                ORDER BY sequence_id
                """,
                (self.thread_id, after_sequence),
            ).fetchall()
        for row in rows:
            history.extend(json.loads(row["input_json"]))
            history.extend(json.loads(row["output_json"]))
        return history

    def iter_events(self) -> Iterable[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, payload_json, created_at FROM events
                WHERE thread_id = ? ORDER BY event_id
                """,
                (self.thread_id,),
            ).fetchall()
        return [
            {
                "timestamp": row["created_at"],
                "type": row["event_type"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def set_replay_after_compaction(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE threads
                SET last_completed_response_id = NULL,
                    replay_history_on_next_request = 1,
                    updated_at = ? WHERE thread_id = ?
                """,
                (_utc_now(), self.thread_id),
            )

    def _ensure_thread(self) -> None:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO threads(
                    thread_id, created_at, updated_at
                ) VALUES (?, ?, ?)
                """,
                (self.thread_id, now, now),
            )

    def _migrate_legacy_files(self) -> None:
        migration_key = f"legacy_migrated:{self.thread_id}"
        with self.database.connect() as connection:
            migrated = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (migration_key,),
            ).fetchone()
        if migrated is not None:
            return

        state_data: dict[str, Any] = {}
        if self.state_path.exists():
            state_data = json.loads(self.state_path.read_text(encoding="utf-8"))
        legacy_events: list[dict[str, Any]] = []
        if self.events_path.exists():
            for line_number, line in enumerate(
                self.events_path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                try:
                    legacy_events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"无效 JSONL：{self.events_path}:{line_number}"
                    ) from exc

        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            if state_data:
                active_turn_id: str | None = None
                pending = state_data.get("pending_tool_calls") or {}
                if pending:
                    active_turn_id = f"legacy-{uuid.uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO turns(
                            turn_id, thread_id, user_input, status,
                            started_at, updated_at
                        ) VALUES (?, ?, '', ?, ?, ?)
                        """,
                        (
                            active_turn_id,
                            self.thread_id,
                            str(state_data.get("status", "aborted")),
                            now,
                            now,
                        ),
                    )
                    for call in pending.values():
                        self._upsert_call(connection, call, active_turn_id, now)
                connection.execute(
                    """
                    UPDATE threads SET last_completed_response_id = ?, status = ?,
                        replay_history_on_next_request = ?, active_turn_id = ?,
                        updated_at = ? WHERE thread_id = ?
                    """,
                    (
                        state_data.get("last_completed_response_id"),
                        str(state_data.get("status", "idle")),
                        int(bool(state_data.get("replay_history_on_next_request"))),
                        active_turn_id,
                        now,
                        self.thread_id,
                    ),
                )
            for event in legacy_events:
                event_type = str(event.get("type", "legacy_event"))
                timestamp = str(event.get("timestamp") or now)
                payload = {
                    key: value
                    for key, value in event.items()
                    if key not in {"type", "timestamp"}
                }
                connection.execute(
                    """
                    INSERT INTO events(
                        thread_id, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (self.thread_id, event_type, _json(payload), timestamp),
                )
                if event_type == "exchange":
                    connection.execute(
                        """
                        INSERT INTO responses(
                            thread_id, response_id, input_json, output_json,
                            output_text, created_at
                        ) VALUES (?, ?, ?, ?, '', ?)
                        """,
                        (
                            self.thread_id,
                            event.get("response_id"),
                            _json(event.get("input", [])),
                            _json(event.get("output", [])),
                            timestamp,
                        ),
                    )
                elif event_type == "compaction":
                    last_response = connection.execute(
                        "SELECT MAX(sequence_id) AS value FROM responses "
                        "WHERE thread_id = ?",
                        (self.thread_id,),
                    ).fetchone()["value"]
                    connection.execute(
                        """
                        INSERT INTO compactions(
                            thread_id, source_response_end, summary,
                            source_input_tokens, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            self.thread_id,
                            last_response,
                            str(event.get("summary", "")),
                            int(event.get("source_input_tokens", 0)),
                            timestamp,
                        ),
                    )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (migration_key, now),
            )

    def _upsert_call(
        self,
        connection: Any,
        call: dict[str, Any],
        turn_id: str | None,
        now: str,
        response_id: str | None = None,
    ) -> None:
        arguments = call.get("arguments", "{}")
        arguments_json = (
            arguments if isinstance(arguments, str) else _json(arguments)
        )
        fingerprint = str(
            call.get("fingerprint")
            or tool_call_fingerprint(str(call.get("name", "")), arguments_json)
        )
        output = call.get("output")
        output_hash = (
            hashlib.sha256(str(output).encode("utf-8")).hexdigest()
            if output is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO tool_calls(
                thread_id, call_id, turn_id, response_id, name,
                arguments_json, fingerprint, status, output_text, output_hash,
                exit_code, stdout_ref, stderr_ref, created_at,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, call_id) DO UPDATE SET
                status = excluded.status,
                output_text = excluded.output_text,
                output_hash = excluded.output_hash,
                exit_code = excluded.exit_code,
                stdout_ref = excluded.stdout_ref,
                stderr_ref = excluded.stderr_ref,
                started_at = COALESCE(excluded.started_at, tool_calls.started_at),
                completed_at = COALESCE(excluded.completed_at, tool_calls.completed_at)
            """,
            (
                self.thread_id,
                str(call["call_id"]),
                turn_id,
                response_id,
                str(call.get("name", "")),
                arguments_json,
                fingerprint,
                str(call.get("status", "pending")),
                str(output) if output is not None else None,
                output_hash,
                call.get("exit_code"),
                call.get("stdout_ref"),
                call.get("stderr_ref"),
                now,
                now if call.get("status") == "running" else None,
                now if call.get("status") in _TERMINAL_TOOL_STATUSES else None,
            ),
        )

    @staticmethod
    def _call_row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "call_id": row["call_id"],
            "name": row["name"],
            "arguments": row["arguments_json"],
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "output": row["output_text"],
            "exit_code": row["exit_code"],
            "stdout_ref": row["stdout_ref"],
            "stderr_ref": row["stderr_ref"],
        }

    def _append_audit_record(self, record: dict[str, Any]) -> None:
        try:
            self.thread_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_audit_if_needed()
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(_json(redact_data(record)) + "\n")
        except OSError:
            # SQLite is authoritative; an optional audit export must not roll
            # back or invalidate already committed runtime state.
            return

    def _rotate_audit_if_needed(self) -> None:
        if not self.events_path.exists():
            return
        if self.events_path.stat().st_size < self.event_log_max_bytes:
            return
        for number in range(self.event_log_backups, 0, -1):
            source = self.events_path.with_name(
                f"{self.events_path.name}.{number}"
            )
            if not source.exists():
                continue
            if number == self.event_log_backups:
                source.unlink()
            else:
                source.replace(
                    self.events_path.with_name(
                        f"{self.events_path.name}.{number + 1}"
                    )
                )
        self.events_path.replace(
            self.events_path.with_name(f"{self.events_path.name}.1")
        )


def tool_call_fingerprint(name: str, arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = arguments.strip()
    canonical = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_item_to_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    if isinstance(item, dict):
        return item
    return dict(item)


def _input_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    value = usage.get("input_tokens") if isinstance(usage, dict) else getattr(
        usage, "input_tokens", None
    )
    return int(value) if value is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
