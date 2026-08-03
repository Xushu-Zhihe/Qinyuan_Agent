from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1


class RuntimeDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"状态数据库版本 {version} 高于程序支持版本 {SCHEMA_VERSION}"
                )
            if version == 0:
                self._create_v1(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _create_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                last_completed_response_id TEXT,
                status TEXT NOT NULL DEFAULT 'idle',
                replay_history_on_next_request INTEGER NOT NULL DEFAULT 0,
                last_input_tokens INTEGER,
                active_turn_id TEXT,
                paused_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                user_input TEXT NOT NULL,
                status TEXT NOT NULL,
                tool_rounds INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                pause_reason TEXT,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS responses (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                turn_id TEXT,
                response_id TEXT,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                output_text TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
                FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
                thread_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                turn_id TEXT,
                response_id TEXT,
                name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                output_text TEXT,
                output_hash TEXT,
                exit_code INTEGER,
                stdout_ref TEXT,
                stderr_ref TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                submitted_at TEXT,
                PRIMARY KEY(thread_id, call_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
                FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
            );
            CREATE TABLE IF NOT EXISTS tool_results (
                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                status TEXT NOT NULL,
                output_text TEXT NOT NULL,
                exit_code INTEGER,
                stdout_ref TEXT,
                stderr_ref TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id, call_id)
                    REFERENCES tool_calls(thread_id, call_id)
            );
            CREATE TABLE IF NOT EXISTS compactions (
                compaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                source_response_start INTEGER,
                source_response_end INTEGER,
                summary TEXT NOT NULL,
                source_input_tokens INTEGER NOT NULL,
                summary_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                turn_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS leases (
                thread_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                process_id INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            );
            CREATE INDEX IF NOT EXISTS idx_responses_thread_sequence
                ON responses(thread_id, sequence_id);
            CREATE INDEX IF NOT EXISTS idx_tool_calls_turn_fingerprint
                ON tool_calls(thread_id, turn_id, fingerprint);
            CREATE INDEX IF NOT EXISTS idx_events_thread_event
                ON events(thread_id, event_id);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_versions(version, applied_at) "
            "VALUES(1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
