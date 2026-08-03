from __future__ import annotations

import asyncio
import ctypes
import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from qsubagent.agent import AgentRunner
from qsubagent.config import (
    AppSettings,
    CapabilitySettings,
    ModelSettings,
    RuntimeSettings,
    ArtifactSettings,
)
from qsubagent.runtime import ThreadRepository
from tools.powershell import POWERSHELL_SCHEMA, powershell
from tools.registry import ToolRegistry


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.queued = deque(responses)

    async def create(self, **_: Any) -> Any:
        return self.queued.popleft()


class PowerShellInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_terminates_registered_process(self) -> None:
        try:
            probe = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "pass",
                stdout=asyncio.subprocess.PIPE,
            )
            await probe.wait()
        except PermissionError as exc:
            self.skipTest(f"sandbox blocks async subprocess pipes: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            escaped_pid_path = str(child_pid_path).replace("'", "''")
            settings = AppSettings(
                model=ModelSettings(
                    name="test-model",
                    api_key="test-key",
                    base_url="",
                ),
                capabilities=CapabilitySettings(
                    previous_response_id=True,
                    local_compaction=False,
                ),
                runtime=RuntimeSettings(
                    state_dir=root,
                    interrupt_grace_seconds=0.2,
                    tool_timeout_seconds=10,
                ),
                artifacts=ArtifactSettings(root=root / "artifacts"),
            )
            tool_call_response = SimpleNamespace(
                id="resp_tool",
                output=[
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "powershell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "$child = Start-Process powershell.exe "
                                    "-ArgumentList '-NoLogo','-NoProfile',"
                                    "'-Command','Start-Sleep -Seconds 30' "
                                    "-PassThru; "
                                    f"Set-Content -LiteralPath '{escaped_pid_path}' "
                                    "-Value $child.Id; "
                                    "[Console]::Out.WriteLine($child.Id); "
                                    "[Console]::Out.Flush(); "
                                    "Wait-Process -Id $child.Id"
                                )
                            }
                        ),
                    }
                ],
                output_text="",
                usage=SimpleNamespace(input_tokens=10),
            )
            client = SimpleNamespace(
                responses=FakeResponses(
                    [
                        tool_call_response,
                        SimpleNamespace(
                            id="resp_after_interrupt",
                            output=[
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": "new turn complete",
                                }
                            ],
                            output_text="new turn complete",
                            usage=SimpleNamespace(input_tokens=10),
                        ),
                    ]
                )
            )
            registry = ToolRegistry()
            registry.register(
                "powershell",
                POWERSHELL_SCHEMA,
                powershell,
                pass_turn=True,
            )
            repository = ThreadRepository(root, "default")
            runner = AgentRunner(
                client=client,
                settings=settings,
                registry=registry,
                repository=repository,
                emit=lambda _: None,
            )

            runner.start_turn("run a slow command")
            process = None
            for _ in range(100):
                turn = runner.active_turn
                process = turn.process if turn is not None else None
                if process is not None:
                    break
                await asyncio.sleep(0.01)

            self.assertIsNotNone(process)
            child_pid = None
            for _ in range(200):
                if child_pid_path.exists():
                    value = child_pid_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    ).strip()
                    if value.isdigit():
                        child_pid = int(value)
                        break
                await asyncio.sleep(0.01)
            if child_pid is None:
                await runner.interrupt()
                await runner.close()
                self.fail("PowerShell child process did not start")
            await runner.interrupt()
            await asyncio.sleep(0)

            assert process is not None
            self.assertIsNotNone(process.returncode)
            state = repository.load_state()
            self.assertEqual(state.status, "aborted")
            self.assertEqual(
                state.pending_tool_calls["call_1"]["status"],
                "outcome_unknown",
            )
            stdout_ref = state.pending_tool_calls["call_1"]["stdout_ref"]
            self.assertIsNotNone(stdout_ref)
            artifact = runner.artifact_store.read(str(stdout_ref))
            self.assertEqual(int(artifact["content"].strip()), child_pid)
            assert child_pid is not None
            for _ in range(50):
                if not process_is_alive(child_pid):
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(process_is_alive(child_pid))
            runner.start_turn("start a new turn")
            assert runner.active_turn is not None
            assert runner.active_turn.task is not None
            await runner.active_turn.task
            self.assertEqual(repository.load_state().status, "idle")
            await runner.close()


def process_is_alive(process_id: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
