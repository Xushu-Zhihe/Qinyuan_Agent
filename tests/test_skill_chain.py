from __future__ import annotations

import asyncio
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
    ArtifactSettings,
    CapabilitySettings,
    ModelSettings,
    RuntimeSettings,
)
from qsubagent.runtime import ActiveTurn, ThreadRepository
from tools.powershell import POWERSHELL_SCHEMA, powershell
from tools.read_skill import (
    LIST_SKILLS_SCHEMA,
    READ_SKILL_SCHEMA,
    list_skills,
    read_skill,
)
from tools.registry import ToolRegistry


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)

    async def create(self, **_: Any) -> Any:
        return self.responses.popleft()


def call(response_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=response_id,
        output=[
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=10),
    )


class SkillToolChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_read_then_execute_powershell(self) -> None:
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
            responses = FakeResponses(
                [
                    call("r1", "c1", "list_skills", {}),
                    call(
                        "r2",
                        "c2",
                        "read_skill",
                        {"name": "workspace-inspection"},
                    ),
                    call(
                        "r3",
                        "c3",
                        "powershell",
                        {"command": "Write-Output SKILL_CHAIN_OK"},
                    ),
                    SimpleNamespace(
                        id="r4",
                        output=[
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": "done",
                            }
                        ],
                        output_text="done",
                        usage=SimpleNamespace(input_tokens=10),
                    ),
                ]
            )
            registry = ToolRegistry()
            registry.register(
                "list_skills",
                LIST_SKILLS_SCHEMA,
                list_skills,
                read_only=True,
            )
            registry.register(
                "read_skill",
                READ_SKILL_SCHEMA,
                read_skill,
                read_only=True,
            )
            registry.register(
                "powershell",
                POWERSHELL_SCHEMA,
                powershell,
                pass_turn=True,
                cancellable=True,
                side_effect=True,
            )
            repository = ThreadRepository(root, "skill-chain")
            settings = AppSettings(
                model=ModelSettings("fake", "fake", ""),
                capabilities=CapabilitySettings(local_compaction=False),
                runtime=RuntimeSettings(
                    thread_id="skill-chain",
                    state_dir=root,
                    tool_timeout_seconds=10,
                ),
                artifacts=ArtifactSettings(root=root / "artifacts"),
            )
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=settings,
                registry=registry,
                repository=repository,
                emit=lambda _: None,
            )

            final = await runner.continue_turn(ActiveTurn(), "inspect")

            self.assertEqual(final.id, "r4")
            with repository.database.connect() as connection:
                rows = connection.execute(
                    "SELECT name, status, output_text FROM tool_calls "
                    "WHERE thread_id = ? ORDER BY created_at",
                    ("skill-chain",),
                ).fetchall()
            self.assertEqual(
                [row["name"] for row in rows],
                ["list_skills", "read_skill", "powershell"],
            )
            self.assertTrue(all(row["status"] == "completed" for row in rows))
            self.assertIn("SKILL_CHAIN_OK", rows[-1]["output_text"])
            await runner.close()
