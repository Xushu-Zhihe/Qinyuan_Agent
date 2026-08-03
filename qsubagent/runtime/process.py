from __future__ import annotations

import asyncio
import ctypes
import os
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ArtifactCapture, ArtifactStore
from .models import ActiveTurn, ToolResult


@dataclass
class ProcessController:
    process: asyncio.subprocess.Process
    job: "WindowsJobObject | None" = None

    async def terminate_tree(self) -> None:
        if self.job is not None:
            self.job.close()
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.kill()
        with suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(self.process.wait(), timeout=5.0)

    def close(self) -> None:
        if self.job is not None:
            self.job.close()


async def run_streaming_process(
    executable: str,
    arguments: list[str],
    *,
    cwd: Path,
    turn: ActiveTurn,
    artifact_store: ArtifactStore,
    stdout_name: str = "stdout.log",
    stderr_name: str = "stderr.log",
) -> ToolResult:
    if not turn.turn_id:
        raise RuntimeError("工具执行缺少 turn_id")
    stdout_capture = artifact_store.create_capture(
        turn.thread_id, turn.turn_id, stdout_name
    )
    stderr_capture = artifact_store.create_capture(
        turn.thread_id, turn.turn_id, stderr_name
    )
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job = WindowsJobObject.assign(process.pid) if os.name == "nt" else None
    job_assignment_failed = os.name == "nt" and job is None
    controller = ProcessController(process=process, job=job)
    turn.process = process
    turn.process_controller = controller

    with stdout_capture, stderr_capture:
        stdout_task = asyncio.create_task(
            _drain_stream(process.stdout, stdout_capture)
        )
        stderr_task = asyncio.create_task(
            _drain_stream(process.stderr, stderr_capture)
        )
        timed_out = False
        cancelled = False
        try:
            if job_assignment_failed:
                await controller.terminate_tree()
            else:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=turn.tool_timeout_seconds,
                )
        except asyncio.TimeoutError:
            timed_out = True
            await controller.terminate_tree()
        except asyncio.CancelledError:
            cancelled = True
            await controller.terminate_tree()
        finally:
            with suppress(Exception):
                await asyncio.gather(stdout_task, stderr_task)
            controller.close()
            if turn.process is process:
                turn.process = None
                turn.process_controller = None

    stdout_summary = stdout_capture.summary()
    stderr_summary = stderr_capture.summary()
    stdout_ref = artifact_store.reference(stdout_capture.path)
    stderr_ref = artifact_store.reference(stderr_capture.path)
    cancelled = cancelled or turn.cancel_event.is_set()
    status = "outcome_unknown" if (
        timed_out or cancelled or job_assignment_failed
    ) else (
        "completed" if process.returncode == 0 else "failed"
    )
    output = _process_summary(
        exit_code=process.returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        job_assignment_failed=job_assignment_failed,
        stdout_ref=stdout_ref,
        stderr_ref=stderr_ref,
        stdout_summary=stdout_summary,
        stderr_summary=stderr_summary,
    )
    artifact_store.prune(turn.thread_id)
    result = ToolResult(
        output=output,
        status=status,
        exit_code=process.returncode,
        stdout_ref=stdout_ref,
        stderr_ref=stderr_ref,
        metadata={
            "timed_out": timed_out,
            "cancelled": cancelled,
            "job_assignment_failed": job_assignment_failed,
            "stdout": stdout_summary,
            "stderr": stderr_summary,
            "job_object": bool(job),
        },
    )
    if cancelled:
        turn.interrupted_tool_result = result
        raise asyncio.CancelledError
    return result


async def _drain_stream(
    reader: asyncio.StreamReader | None,
    capture: ArtifactCapture,
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            return
        capture.write(chunk)


def _process_summary(
    *,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
    job_assignment_failed: bool,
    stdout_ref: str,
    stderr_ref: str,
    stdout_summary: dict[str, Any],
    stderr_summary: dict[str, Any],
) -> str:
    return (
        f"status: "
        f"{'outcome_unknown' if timed_out or cancelled or job_assignment_failed else 'finished'}\n"
        f"exit_code: {exit_code}\n"
        f"timed_out: {str(timed_out).lower()}\n"
        f"cancelled: {str(cancelled).lower()}\n"
        f"job_assignment_failed: {str(job_assignment_failed).lower()}\n"
        f"stdout_bytes: {stdout_summary['total_bytes']}\n"
        f"stderr_bytes: {stderr_summary['total_bytes']}\n"
        f"artifact_truncated: "
        f"{str(stdout_summary['artifact_truncated'] or stderr_summary['artifact_truncated']).lower()}\n"
        f"stdout_artifact: {stdout_ref}\n"
        f"stderr_artifact: {stderr_ref}\n"
        f"stdout_head:\n{stdout_summary['head'] or '(empty)'}\n"
        f"stdout_tail:\n{stdout_summary['tail'] or '(empty)'}\n"
        f"stderr_head:\n{stderr_summary['head'] or '(empty)'}\n"
        f"stderr_tail:\n{stderr_summary['tail'] or '(empty)'}"
    )


class WindowsJobObject:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def assign(cls, process_id: int) -> "WindowsJobObject | None":
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            return None

        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = kernel32.SetInformationJobObject(
            job_handle,
            cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        process_handle = kernel32.OpenProcess(
            cls.PROCESS_TERMINATE | cls.PROCESS_SET_QUOTA,
            False,
            process_id,
        )
        assigned = bool(
            configured
            and process_handle
            and kernel32.AssignProcessToJobObject(job_handle, process_handle)
        )
        if process_handle:
            kernel32.CloseHandle(process_handle)
        if not assigned:
            kernel32.CloseHandle(job_handle)
            return None
        return cls(job_handle)

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)
        self.handle = 0


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]
