from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from qsubagent.runtime import ThreadRepository


CHILD_SCRIPT = r"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from qsubagent.runtime import ThreadRepository, ToolResult

root = Path(sys.argv[1])
thread_id = sys.argv[2]
stage = sys.argv[3]
repository = ThreadRepository(root, thread_id)
repository.acquire_lease()
turn_id = repository.start_turn('crash test')
if stage == 'turn_started':
    os._exit(7)
response = SimpleNamespace(
    id='response-1',
    output=[],
    output_text='',
    usage=SimpleNamespace(input_tokens=10),
)
repository.record_response(
    turn_id=turn_id,
    request_input=[{'role': 'user', 'content': 'crash test'}],
    response=response,
    pending_calls={
        'call-1': {
            'call_id': 'call-1',
            'name': 'powershell',
            'arguments': '{"command":"Write-Output once"}',
            'status': 'pending',
            'output': None,
        }
    },
)
if stage == 'response_committed':
    os._exit(7)
repository.mark_tool_running('call-1')
if stage == 'tool_running':
    os._exit(7)
repository.finish_tool_call(
    'call-1',
    ToolResult(output='once', status='completed'),
)
os._exit(7)
"""


class ForcedCrashRecoveryTests(unittest.TestCase):
    def test_forced_exit_stages_are_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in (
                "turn_started",
                "response_committed",
                "tool_running",
                "tool_completed",
            ):
                thread_id = stage.replace("_", "-")
                try:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            CHILD_SCRIPT,
                            str(root),
                            thread_id,
                            stage,
                        ],
                        cwd=Path(__file__).resolve().parent.parent,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except PermissionError as exc:
                    self.skipTest(f"sandbox blocks subprocesses: {exc}")
                self.assertEqual(completed.returncode, 7, completed.stderr)

                repository = ThreadRepository(root, thread_id)
                repository.acquire_lease()
                try:
                    repository.recover_interrupted()
                    state = repository.load_state()
                    if stage == "turn_started":
                        self.assertEqual(state.status, "aborted")
                        self.assertEqual(state.pending_tool_calls, {})
                    elif stage == "response_committed":
                        self.assertEqual(
                            state.pending_tool_calls["call-1"]["status"],
                            "pending",
                        )
                    elif stage == "tool_running":
                        self.assertEqual(
                            state.pending_tool_calls["call-1"]["status"],
                            "outcome_unknown",
                        )
                    else:
                        self.assertEqual(
                            state.pending_tool_calls["call-1"]["status"],
                            "completed",
                        )
                finally:
                    repository.release_lease()
