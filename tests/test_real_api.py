from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from openai import AsyncOpenAI

from qsubagent.agent import AgentRunner
from qsubagent.config import (
    AppSettings,
    ArtifactSettings,
    CapabilitySettings,
    ModelSettings,
    RuntimeSettings,
)
from qsubagent.runtime import ActiveTurn, ThreadRepository
from tools import load_builtin_tools
from tools.registry import registry


RUN_REAL = os.environ.get("QSUBAGENT_RUN_REAL_API") == "1"


@unittest.skipUnless(
    RUN_REAL,
    "set QSUBAGENT_RUN_REAL_API=1 to run the real Responses API workflow",
)
class RealResponsesWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_interrupt_restart_recovery_workflow(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("QSUBAGENT_MODEL", "")
        if not api_key or not model:
            self.fail("OPENAI_API_KEY and QSUBAGENT_MODEL are required")
        base_url = os.environ.get("QSUBAGENT_BASE_URL", "")
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=90,
            max_retries=0,
        )
        load_builtin_tools()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AppSettings(
                model=ModelSettings(model, api_key, base_url),
                capabilities=CapabilitySettings(
                    previous_response_id=True,
                    prompt_cache_key=True,
                    local_compaction=False,
                ),
                runtime=RuntimeSettings(
                    state_dir=root,
                    thread_id="real-smoke",
                    model_timeout_seconds=90,
                    tool_timeout_seconds=60,
                ),
                artifacts=ArtifactSettings(root=root / "artifacts"),
            )
            first_repository = ThreadRepository(root, "real-smoke")
            first = AgentRunner(
                client=client,
                settings=settings,
                registry=registry,
                repository=first_repository,
                emit=lambda _: None,
            )
            first.start_turn(
                "依次调用 list_skills、read_skill（读取 workspace-inspection），"
                "然后调用 powershell 执行 Start-Sleep -Seconds 30。必须调用工具。"
            )
            for _ in range(600):
                active = first.active_turn
                if active is not None and active.process is not None:
                    break
                if active is None:
                    self.fail("model ended before starting PowerShell")
                await asyncio.sleep(0.1)
            else:
                self.fail("PowerShell did not start within 60 seconds")

            await first.interrupt()
            self.assertIn(
                "outcome_unknown",
                {
                    call["status"]
                    for call in first.repository.load_state().pending_tool_calls.values()
                },
            )
            await first.close()

            second_repository = ThreadRepository(root, "real-smoke")
            second = AgentRunner(
                client=client,
                settings=settings,
                registry=registry,
                repository=second_repository,
                emit=lambda _: None,
            )
            response = await second.continue_turn(
                ActiveTurn(),
                "从中断状态继续，不要重试睡眠命令。调用 powershell 执行 "
                "Write-Output QSUBAGENT_REAL_RECOVERY_OK，然后完成任务。",
            )
            self.assertIsNotNone(response)
            with second_repository.database.connect() as connection:
                names = [
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM tool_calls WHERE thread_id = ?",
                        ("real-smoke",),
                    )
                ]
            self.assertGreaterEqual(names.count("powershell"), 2)
            self.assertIn("list_skills", names)
            self.assertIn("read_skill", names)
            await second.close()
        await client.close()
