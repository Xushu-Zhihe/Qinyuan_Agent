from __future__ import annotations

import os
import time
import uuid
import ctypes
from ctypes import wintypes

from .database import RuntimeDatabase


class ThreadLeaseError(RuntimeError):
    pass


class ThreadLease:
    def __init__(
        self,
        database: RuntimeDatabase,
        thread_id: str,
        *,
        ttl_seconds: float = 30.0,
    ) -> None:
        self.database = database
        self.thread_id = thread_id
        self.ttl_seconds = ttl_seconds
        self.owner_id = uuid.uuid4().hex
        self.process_id = os.getpid()
        self.acquired = False

    def acquire(self) -> None:
        now = time.time()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT owner_id, process_id, expires_at FROM leases "
                "WHERE thread_id = ?",
                (self.thread_id,),
            ).fetchone()
            lease_is_live = (
                row is not None
                and float(row["expires_at"]) > now
                and _process_is_alive(int(row["process_id"]))
            )
            if lease_is_live:
                raise ThreadLeaseError(
                    "线程已被另一个实例占用："
                    f"thread_id={self.thread_id}, pid={row['process_id']}"
                )
            connection.execute(
                "DELETE FROM leases WHERE thread_id = ?",
                (self.thread_id,),
            )
            timestamp = _utc_now()
            connection.execute(
                """
                INSERT INTO leases(
                    thread_id, owner_id, process_id, acquired_at,
                    heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.thread_id,
                    self.owner_id,
                    self.process_id,
                    timestamp,
                    timestamp,
                    now + self.ttl_seconds,
                ),
            )
        self.acquired = True

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        now = time.time()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE thread_id = ? AND owner_id = ?
                """,
                (
                    _utc_now(),
                    now + self.ttl_seconds,
                    self.thread_id,
                    self.owner_id,
                ),
            )
            if cursor.rowcount != 1:
                self.acquired = False
                raise ThreadLeaseError("线程租约已丢失，停止继续写入")

    def release(self) -> None:
        if not self.acquired:
            return
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM leases WHERE thread_id = ? AND owner_id = ?",
                (self.thread_id, self.owner_id),
            )
        self.acquired = False


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
