from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from qsubagent.approval import ApprovalManager
from qsubagent.config import ArtifactSettings
from qsubagent.runtime import ActiveTurn, ArtifactStore
from tools.powershell import powershell


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_can_be_approved(self) -> None:
        output: list[str] = []
        manager = ApprovalManager(emit=output.append)
        cancel_event = asyncio.Event()
        task = asyncio.create_task(manager.request("Write-Output exact", cancel_event))
        await asyncio.sleep(0)
        request_id = manager.pending_ids()[0]

        self.assertTrue(manager.decide(request_id, True))
        self.assertTrue(await task)
        self.assertIn("Write-Output exact", output)

    async def test_no_approval_channel_denies_without_execution(self) -> None:
        manager = ApprovalManager(channel_available=False)
        turn = ActiveTurn(
            turn_id="turn",
            execution_mode="approval",
            approval_manager=manager,
        )

        result = await powershell("Write-Output should-not-run", turn)

        self.assertEqual(result.status, "failed")
        self.assertIn("approval_channel_unavailable", result.output)

    async def test_interrupt_cancels_waiting_approval(self) -> None:
        manager = ApprovalManager(emit=lambda _: None)
        cancel_event = asyncio.Event()
        task = asyncio.create_task(manager.request("command", cancel_event))
        await asyncio.sleep(0)

        cancel_event.set()

        self.assertFalse(await task)
        self.assertEqual(manager.pending_ids(), [])

    async def test_approved_command_executes_exact_text(self) -> None:
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
            emitted: list[str] = []
            manager = ApprovalManager(emit=emitted.append)
            turn = ActiveTurn(
                turn_id="turn",
                thread_id="thread",
                execution_mode="approval",
                approval_manager=manager,
                artifact_store=ArtifactStore(
                    ArtifactSettings(root=Path(directory))
                ),
            )
            command = "Write-Output APPROVAL_EXACT_OK"
            task = asyncio.create_task(powershell(command, turn))
            for _ in range(100):
                if manager.pending_ids():
                    break
                await asyncio.sleep(0.01)
            request_id = manager.pending_ids()[0]

            manager.decide(request_id, True)
            result = await task

            self.assertEqual(result.status, "completed")
            self.assertIn("APPROVAL_EXACT_OK", result.output)
            self.assertIn(command, emitted)
