from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qsubagent.runtime import (
    ThreadLeaseError,
    ThreadRepository,
    ThreadState,
    ToolResult,
)


class ThreadRepositoryTests(unittest.TestCase):
    def test_state_round_trip_and_history_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ThreadRepository(Path(directory), "thread-1")
            state = ThreadState(
                thread_id="thread-1",
                last_completed_response_id="resp_1",
                status="running",
            )
            repository.save_state(state)

            self.assertEqual(repository.load_state(), state)

            response = SimpleNamespace(
                id="resp_1",
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "answer",
                    }
                ],
            )
            repository.append_exchange(
                [{"role": "user", "content": "question"}],
                response,
            )

            self.assertEqual(
                repository.rebuild_history(),
                [
                    {"role": "user", "content": "question"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "answer",
                    },
                ],
            )

    def test_compaction_replaces_prior_replay_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ThreadRepository(Path(directory), "thread-1")
            response = SimpleNamespace(
                id="resp_1",
                output=[{"role": "assistant", "content": "old"}],
            )
            repository.append_exchange(
                [{"role": "user", "content": "old question"}],
                response,
            )
            repository.append_compaction("preserved state", 100)

            history = repository.rebuild_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["role"], "user")
            self.assertIn("preserved state", history[0]["content"])

    def test_running_tool_becomes_outcome_unknown_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ThreadRepository(root, "thread-1")
            turn_id = repository.start_turn("run")
            tool_response = SimpleNamespace(
                id="resp_tool",
                output=[],
                output_text="",
                usage=SimpleNamespace(input_tokens=10),
            )
            repository.record_response(
                turn_id=turn_id,
                request_input=[{"role": "user", "content": "run"}],
                response=tool_response,
                pending_calls={
                    "call-1": {
                        "call_id": "call-1",
                        "name": "powershell",
                        "arguments": '{"command":"Write-Output ok"}',
                        "status": "pending",
                        "output": None,
                    }
                },
            )
            repository.mark_tool_running("call-1")

            recovered = repository.recover_interrupted()
            state = repository.load_state()

            self.assertEqual(recovered["tool_calls"], 1)
            self.assertEqual(
                state.pending_tool_calls["call-1"]["status"],
                "outcome_unknown",
            )

    def test_completed_tool_result_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ThreadRepository(root, "thread-1")
            turn_id = repository.start_turn("run")
            tool_response = SimpleNamespace(
                id="resp_tool",
                output=[],
                output_text="",
                usage=SimpleNamespace(input_tokens=10),
            )
            repository.record_response(
                turn_id=turn_id,
                request_input=[],
                response=tool_response,
                pending_calls={
                    "call-1": {
                        "call_id": "call-1",
                        "name": "tool",
                        "arguments": "{}",
                        "status": "pending",
                        "output": None,
                    }
                },
            )
            repository.mark_tool_running("call-1")
            repository.finish_tool_call(
                "call-1",
                ToolResult(output="done", status="completed"),
            )

            reopened = ThreadRepository(root, "thread-1")
            call = reopened.load_state().pending_tool_calls["call-1"]
            self.assertEqual(call["status"], "completed")
            self.assertEqual(call["output"], "done")
            with self.assertRaises(RuntimeError):
                reopened.finish_tool_call(
                    "call-1",
                    ToolResult(output="duplicate", status="completed"),
                )

    def test_thread_lease_blocks_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ThreadRepository(root, "thread-1")
            second = ThreadRepository(root, "thread-1")
            first.acquire_lease()
            try:
                with self.assertRaises(ThreadLeaseError):
                    second.acquire_lease()
            finally:
                first.release_lease()

    def test_legacy_json_state_is_imported_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread_dir = root / "thread-1"
            thread_dir.mkdir(parents=True)
            (thread_dir / "state.json").write_text(
                '{"thread_id":"thread-1","last_completed_response_id":'
                '"resp-old","pending_tool_calls":{},"status":"idle",'
                '"replay_history_on_next_request":false}',
                encoding="utf-8",
            )
            (thread_dir / "events.jsonl").write_text(
                '{"type":"exchange","input":[{"role":"user",'
                '"content":"old"}],"output":[],"response_id":"resp-old"}\n',
                encoding="utf-8",
            )

            repository = ThreadRepository(root, "thread-1")
            reopened = ThreadRepository(root, "thread-1")

            self.assertEqual(
                repository.load_state().last_completed_response_id,
                "resp-old",
            )
            self.assertEqual(len(reopened.rebuild_history()), 1)

    def test_rejects_unsafe_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ThreadRepository(Path(directory), "../outside")


if __name__ == "__main__":
    unittest.main()
