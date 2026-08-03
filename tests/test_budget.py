from __future__ import annotations

import asyncio
import json
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
from tools.registry import ToolRegistry


class HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.queued = deque(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.queued.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class SlowResponses:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        await asyncio.Event().wait()


def message(response_id: str, text: str = "done") -> Any:
    return SimpleNamespace(
        id=response_id,
        output=[{"type": "message", "role": "assistant", "content": text}],
        output_text=text,
        usage=SimpleNamespace(input_tokens=10),
    )


def tool_call(response_id: str, call_id: str) -> Any:
    return SimpleNamespace(
        id=response_id,
        output=[
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "echo",
                "arguments": '{"value":"same"}',
            }
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=10),
    )


def make_settings(
    root: Path,
    *,
    soft: int = 16,
    hard: int = 64,
    repeat: int = 3,
    retries: int = 0,
    turn_timeout: float = 10.0,
    model_timeout: float = 5.0,
    tool_timeout: float = 5.0,
) -> AppSettings:
    return AppSettings(
        model=ModelSettings("test-model", "test-key", ""),
        capabilities=CapabilitySettings(local_compaction=False),
        runtime=RuntimeSettings(
            state_dir=root,
            soft_tool_rounds=soft,
            hard_tool_rounds=hard,
            repeated_call_limit=repeat,
            model_max_retries=retries,
            retry_initial_seconds=0,
            turn_timeout_seconds=turn_timeout,
            model_timeout_seconds=model_timeout,
            tool_timeout_seconds=tool_timeout,
        ),
        artifacts=ArtifactSettings(root=root / "artifacts"),
    )


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_forty_tool_rounds_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queued: list[Any] = []
            for index in range(40):
                queued.append(
                    SimpleNamespace(
                        id=f"r{index}",
                        output=[
                            {
                                "type": "function_call",
                                "call_id": f"c{index}",
                                "name": "echo",
                                "arguments": json.dumps({"value": str(index)}),
                            }
                        ],
                        output_text="",
                        usage=SimpleNamespace(input_tokens=10),
                    )
                )
            queued.append(message("final", "forty complete"))
            responses = FakeResponses(queued)
            executed: list[str] = []
            registry = ToolRegistry()
            registry.register(
                "echo",
                {"type": "function", "name": "echo", "parameters": {}},
                lambda value: executed.append(value) or value,
            )
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, soft=16, hard=64),
                registry=registry,
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            result = await runner.continue_turn(ActiveTurn(), "long task")

            self.assertEqual(result.id, "final")
            self.assertEqual(len(executed), 40)
            self.assertEqual(runner.state.status, "idle")
            await runner.close()

    async def test_hard_tool_round_limit_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses(
                [tool_call("r1", "c1"), tool_call("r2", "c2"), tool_call("r3", "c3")]
            )
            executed: list[str] = []
            registry = ToolRegistry()
            registry.register(
                "echo",
                {"type": "function", "name": "echo", "parameters": {}},
                lambda value: executed.append(value) or value,
            )
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, soft=1, hard=2, repeat=99),
                registry=registry,
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            result = await runner.continue_turn(ActiveTurn(), "loop")

            self.assertIsNone(result)
            self.assertEqual(executed, ["same", "same"])
            self.assertEqual(runner.state.status, "paused")
            self.assertEqual(runner.state.paused_reason, "hard_tool_round_limit")
            await runner.close()
    async def test_repeated_unchanged_call_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses(
                [tool_call("r1", "c1"), tool_call("r2", "c2"), tool_call("r3", "c3")]
            )
            executed = 0

            def echo(value: str) -> str:
                nonlocal executed
                executed += 1
                return value

            registry = ToolRegistry()
            registry.register(
                "echo",
                {"type": "function", "name": "echo", "parameters": {}},
                echo,
            )
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, soft=1, hard=10, repeat=2),
                registry=registry,
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            await runner.continue_turn(ActiveTurn(), "repeat")

            self.assertEqual(executed, 2)
            self.assertEqual(
                runner.state.paused_reason,
                "repeated_tool_call_limit",
            )
            await runner.close()

    async def test_soft_limit_checkpoints_but_allows_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses([tool_call("r1", "c1"), message("r2")])
            registry = ToolRegistry()
            registry.register(
                "echo",
                {"type": "function", "name": "echo", "parameters": {}},
                lambda value: value,
            )
            repository = ThreadRepository(root, "default")
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, soft=1, hard=3),
                registry=registry,
                repository=repository,
                emit=lambda _: None,
            )

            response = await runner.continue_turn(ActiveTurn(), "finish")

            self.assertEqual(response.id, "r2")
            self.assertEqual(runner.state.status, "idle")
            self.assertIn(
                "soft_tool_round_checkpoint",
                [event["type"] for event in repository.iter_events()],
            )
            await runner.close()

    async def test_auth_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses([HttpError(401)])
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, retries=3),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            with self.assertRaisesRegex(RuntimeError, "不会重试"):
                await runner.continue_turn(ActiveTurn(), "auth")
            self.assertEqual(len(responses.calls), 1)
            await runner.close()

    async def test_429_uses_bounded_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses([HttpError(429), message("r1")])
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, retries=1),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            response = await runner.continue_turn(ActiveTurn(), "retry")

            self.assertEqual(response.id, "r1")
            self.assertEqual(len(responses.calls), 2)
            await runner.close()

    async def test_turn_timeout_saves_resumable_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = SlowResponses()
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, turn_timeout=0.05),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            result = await runner.continue_turn(ActiveTurn(), "slow")

            self.assertIsNone(result)
            self.assertEqual(runner.state.status, "paused")
            self.assertEqual(runner.state.paused_reason, "turn_timeout")
            await runner.close()

    async def test_hard_limit_can_be_explicitly_continued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = FakeResponses(
                [
                    tool_call("r1", "c1"),
                    tool_call("r2", "c2"),
                    message("r3", "complete"),
                ]
            )
            executed: list[str] = []
            registry = ToolRegistry()
            registry.register(
                "echo",
                {"type": "function", "name": "echo", "parameters": {}},
                lambda value: executed.append(value) or value,
            )
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, soft=1, hard=1, repeat=99),
                registry=registry,
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            first_result = await runner.continue_turn(ActiveTurn(), "loop")
            resumed = await runner.continue_turn(
                ActiveTurn(),
                None,
                resume=True,
            )

            self.assertIsNone(first_result)
            self.assertEqual(resumed.output_text, "complete")
            self.assertEqual(executed, ["same", "same"])
            self.assertEqual(runner.state.status, "idle")
            await runner.close()

    async def test_model_timeout_pauses_without_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = SlowResponses()
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(
                    root,
                    model_timeout=0.05,
                    turn_timeout=1,
                ),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            result = await runner.continue_turn(ActiveTurn(), "slow")

            self.assertIsNone(result)
            self.assertEqual(responses.calls, 1)
            self.assertEqual(runner.state.paused_reason, "model_timeout")
            await runner.close()

    async def test_side_effect_tool_timeout_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response_with_slow_tool = SimpleNamespace(
                id="r1",
                output=[
                    {
                        "type": "function_call",
                        "call_id": "slow-call",
                        "name": "slow_side_effect",
                        "arguments": "{}",
                    }
                ],
                output_text="",
                usage=SimpleNamespace(input_tokens=10),
            )
            responses = FakeResponses([response_with_slow_tool, message("r2")])
            executions = 0

            async def slow_side_effect() -> str:
                nonlocal executions
                executions += 1
                await asyncio.sleep(30)
                return "late"

            registry = ToolRegistry()
            registry.register(
                "slow_side_effect",
                {
                    "type": "function",
                    "name": "slow_side_effect",
                    "parameters": {},
                },
                slow_side_effect,
                cancellable=True,
                side_effect=True,
            )
            repository = ThreadRepository(root, "default")
            runner = AgentRunner(
                client=SimpleNamespace(responses=responses),
                settings=make_settings(root, tool_timeout=0.05),
                registry=registry,
                repository=repository,
                emit=lambda _: None,
            )

            response = await runner.continue_turn(ActiveTurn(), "run")

            self.assertEqual(response.id, "r2")
            self.assertEqual(executions, 1)
            with repository.database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM tool_calls WHERE call_id = 'slow-call'"
                ).fetchone()["status"]
            self.assertEqual(status, "outcome_unknown")
            await runner.close()
