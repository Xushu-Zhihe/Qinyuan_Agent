from __future__ import annotations

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
)
from qsubagent.runtime import ActiveTurn, ThreadRepository
from tools.registry import ToolRegistry


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.queued = deque(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.queued.popleft()


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = FakeResponses(responses)


class FakeStream:
    def __init__(self, final_response: Any) -> None:
        self.final_response = final_response
        self.events = iter(
            [
                SimpleNamespace(type="response.output_text.delta", delta="hel"),
                SimpleNamespace(type="response.output_text.delta", delta="lo"),
            ]
        )

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self.events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def get_final_response(self) -> Any:
        return self.final_response


class StreamingResponses:
    def __init__(self, final_response: Any) -> None:
        self.final_response = final_response
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return FakeStream(self.final_response)


def response(
    response_id: str,
    text: str,
    *,
    input_tokens: int = 10,
) -> Any:
    return SimpleNamespace(
        id=response_id,
        output=[{"type": "message", "role": "assistant", "content": text}],
        output_text=text,
        usage=SimpleNamespace(input_tokens=input_tokens),
    )


def settings(
    root: Path,
    *,
    previous_response_id: bool,
) -> AppSettings:
    return AppSettings(
        model=ModelSettings(
            name="test-model",
            api_key="test-key",
            base_url="",
        ),
        capabilities=CapabilitySettings(
            previous_response_id=previous_response_id,
            local_compaction=False,
        ),
        runtime=RuntimeSettings(
            state_dir=root,
            context_window=1_000,
        ),
    )


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_response_id_uses_incremental_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient([response("resp_1", "one"), response("resp_2", "two")])
            runner = AgentRunner(
                client=client,
                settings=settings(root, previous_response_id=True),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            await runner.continue_turn(ActiveTurn(), "first")
            await runner.continue_turn(ActiveTurn(), "second")

            first_call, second_call = client.responses.calls
            self.assertNotIn("previous_response_id", first_call)
            self.assertEqual(second_call["previous_response_id"], "resp_1")
            self.assertEqual(
                second_call["input"],
                [{"role": "user", "content": "second"}],
            )

    async def test_local_mode_replays_jsonl_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient([response("resp_1", "one"), response("resp_2", "two")])
            runner = AgentRunner(
                client=client,
                settings=settings(root, previous_response_id=False),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            await runner.continue_turn(ActiveTurn(), "first")
            await runner.continue_turn(ActiveTurn(), "second")

            second_input = client.responses.calls[1]["input"]
            self.assertEqual(second_input[0]["content"], "first")
            self.assertEqual(second_input[-1]["content"], "second")
            self.assertNotIn("previous_response_id", client.responses.calls[1])

    async def test_aborted_tool_call_is_closed_before_new_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ThreadRepository(root, "default")
            state = repository.load_state()
            state.last_completed_response_id = "resp_tool"
            state.pending_tool_calls = {
                "call_1": {
                    "call_id": "call_1",
                    "name": "slow_tool",
                    "arguments": "{}",
                    "status": "aborted",
                    "output": None,
                }
            }
            repository.save_state(state)

            client = FakeClient([response("resp_2", "continued")])
            runner = AgentRunner(
                client=client,
                settings=settings(root, previous_response_id=True),
                registry=ToolRegistry(),
                repository=repository,
                emit=lambda _: None,
            )
            await runner.continue_turn(ActiveTurn(), "continue")

            request_input = client.responses.calls[0]["input"]
            self.assertEqual(request_input[0]["type"], "function_call_output")
            self.assertEqual(request_input[0]["call_id"], "call_1")
            self.assertEqual(request_input[1]["content"], "continue")

    async def test_local_compaction_resets_history_to_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compact_settings = AppSettings(
                model=ModelSettings(
                    name="test-model",
                    api_key="test-key",
                    base_url="",
                ),
                capabilities=CapabilitySettings(
                    previous_response_id=False,
                    server_compaction=False,
                    local_compaction=True,
                ),
                runtime=RuntimeSettings(
                    state_dir=root,
                    context_window=100,
                    compact_ratio=0.9,
                ),
            )
            compacted = response("resp_compact", "preserved state")
            client = FakeClient(
                [
                    response("resp_1", "long answer", input_tokens=95),
                    compacted,
                ]
            )
            repository = ThreadRepository(root, "default")
            runner = AgentRunner(
                client=client,
                settings=compact_settings,
                registry=ToolRegistry(),
                repository=repository,
                emit=lambda _: None,
            )

            await runner.continue_turn(ActiveTurn(), "long conversation")

            history = repository.rebuild_history()
            self.assertEqual(len(client.responses.calls), 2)
            self.assertEqual(len(history), 1)
            self.assertIn("preserved state", history[0]["content"])
            self.assertIsNone(repository.load_state().last_completed_response_id)

    async def test_server_compaction_is_sent_as_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_settings = AppSettings(
                model=ModelSettings(
                    name="test-model",
                    api_key="test-key",
                    base_url="",
                ),
                capabilities=CapabilitySettings(
                    previous_response_id=True,
                    server_compaction=True,
                    local_compaction=False,
                ),
                runtime=RuntimeSettings(
                    state_dir=root,
                    context_window=1_000,
                    compact_ratio=0.9,
                ),
            )
            client = FakeClient([response("resp_1", "answer")])
            runner = AgentRunner(
                client=client,
                settings=server_settings,
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            await runner.continue_turn(ActiveTurn(), "question")

            self.assertEqual(
                client.responses.calls[0]["context_management"],
                [{"type": "compaction", "compact_threshold": 900}],
            )

    async def test_one_hundred_incremental_turns_do_not_replay_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(
                [response(f"resp_{index}", str(index)) for index in range(100)]
            )
            runner = AgentRunner(
                client=client,
                settings=settings(root, previous_response_id=True),
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=lambda _: None,
            )

            for index in range(100):
                await runner.continue_turn(ActiveTurn(), f"turn {index}")

            self.assertEqual(len(client.responses.calls), 100)
            for index, call in enumerate(client.responses.calls):
                self.assertEqual(
                    call["input"],
                    [{"role": "user", "content": f"turn {index}"}],
                )
                if index:
                    self.assertEqual(
                        call["previous_response_id"],
                        f"resp_{index - 1}",
                    )
            await runner.close()

    async def test_streaming_response_emits_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            streamed = StreamingResponses(response("resp_1", "hello"))
            stream_settings = AppSettings(
                model=ModelSettings("test-model", "test-key", ""),
                capabilities=CapabilitySettings(
                    previous_response_id=True,
                    local_compaction=False,
                    streaming=True,
                ),
                runtime=RuntimeSettings(state_dir=root),
            )
            emitted: list[str] = []
            runner = AgentRunner(
                client=SimpleNamespace(responses=streamed),
                settings=stream_settings,
                registry=ToolRegistry(),
                repository=ThreadRepository(root, "default"),
                emit=emitted.append,
            )

            result = await runner.continue_turn(ActiveTurn(), "stream")

            self.assertEqual(result.output_text, "hello")
            self.assertEqual(emitted, ["hel", "lo"])
            await runner.close()


if __name__ == "__main__":
    unittest.main()
