from __future__ import annotations

import asyncio
import io
import shutil
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from Agents.agent import (
    PROJECT_ROOT,
    SkillAgentFactory,
    ToolCallingAgentRunner,
)
from agent import (
    EXECUTION_SYSTEM_PROMPT,
    EXECUTION_PREFIX,
    PLANNING_PREFIX,
    PLANNING_SYSTEM_PROMPT,
    TIMEOUT_SYSTEM_PROMPT,
    TIMEOUT_PREFIX,
    AgentInterrupted,
    _execution_identity,
    _load_execution_runtime,
    _normalize_provider_output,
    _run_protocol_correction,
    _optional_csv_environment,
    _optional_path_argument,
    _run_request_with_signal_handlers,
    _stage_requested_execution_logs,
    classify_request,
    main,
    run_request,
)
from langchain_core.tools import StructuredTool


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls: list[dict[str, object]] = []


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Response(self.content)


class _SequencedLLM:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0
        self.message_snapshots = []

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, messages):
        self.message_snapshots.append(list(messages))
        response = self.responses[self.calls]
        self.calls += 1
        return response


class _ToolResponse(_Response):
    def __init__(self, content: str, tool_calls: list[dict[str, object]]) -> None:
        super().__init__(content)
        self.tool_calls = tool_calls


class AgentCliTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _execution_request() -> str:
        return (
            EXECUTION_PREFIX
            + '\nAssigned intent:\n{"id":"intent-1"}\n\n'
            + 'Graph snapshot:\n{"nodes":[{"id":"goal-1","type":"goal"}],"edges":[]}'
        )

    @staticmethod
    def _valid_v2_result() -> str:
        return (
            'BLACKBOARD_RESULT: '
            '{"protocol_version":2,"intent_id":"intent-1","facts":[],'
            '"edges":[],"goal_achieved":false,"goal_result_ref":null}'
        )

    def test_prompts_distinguish_intent_completion_from_goal_completion(self) -> None:
        self.assertIn("An Intent being completed never means", PLANNING_SYSTEM_PROMPT)
        self.assertIn("There are two separate completion decisions", EXECUTION_SYSTEM_PROMPT)
        self.assertIn("goal_achieved` to false", EXECUTION_SYSTEM_PROMPT)
        self.assertIn("whole Goal", TIMEOUT_SYSTEM_PROMPT)

    def test_classifies_qinyuan_bat_request_headers(self) -> None:
        self.assertEqual(classify_request(PLANNING_PREFIX + "\nbody"), "planning")
        self.assertEqual(classify_request(EXECUTION_PREFIX + "\nbody"), "execution")
        self.assertEqual(classify_request(TIMEOUT_PREFIX + "\nbody"), "timeout")

    def test_rejects_unknown_request_header(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported qinyuan-bat header"):
            classify_request("unrecognized request")

    async def test_planning_request_runs_without_tools(self) -> None:
        llm = _FakeLLM("BLACKBOARD_INTENT: {\"action\":\"idle\",\"reason\":\"done\"}")
        result = await run_request(PLANNING_PREFIX + "\nbody", llm=llm)
        self.assertIn("BLACKBOARD_INTENT:", result)
        self.assertEqual(llm.calls, 1)

    async def test_execution_runner_requires_tool_before_final_response(self) -> None:
        tool = StructuredTool.from_function(
            lambda: {"status": "completed", "stdout": "verified"},
            name="probe",
            description="Run the verification probe",
        )
        llm = _SequencedLLM(
            [
                _Response(
                    'BLACKBOARD_RESULT: {"goal_achieved":true,'
                    '"final_answer":"a proposed next step"}'
                ),
                _ToolResponse(
                    "",
                    [{"name": "probe", "args": {}, "id": "call-1"}],
                ),
                _Response(
                    'BLACKBOARD_RESULT: {"goal_achieved":false,'
                    '"final_answer":null}'
                ),
            ]
        )
        runner = ToolCallingAgentRunner(
            llm=llm,
            system_prompt="execution",
            tools=[tool],
            max_steps=3,
            require_tool_call_before_final=True,
        )

        result = await runner.arun("execute")

        self.assertIn('"goal_achieved":false', result)
        self.assertEqual(llm.calls, 3)

    async def test_execution_runner_rejects_repeated_unverified_completion(
        self,
    ) -> None:
        llm = _FakeLLM(
            'BLACKBOARD_RESULT: {"goal_achieved":true,'
            '"final_answer":"a proposed next step"}'
        )
        runner = ToolCallingAgentRunner(
            llm=llm,
            system_prompt="execution",
            tools=[],
            max_steps=2,
            require_tool_call_before_final=True,
        )

        with self.assertRaisesRegex(RuntimeError, "maximum step count"):
            await runner.arun("execute")

        self.assertEqual(llm.calls, 2)

    async def test_empty_result_requires_another_real_tool_call(self) -> None:
        calls: list[str] = []
        no_op = StructuredTool.from_function(
            lambda: calls.append("no-op") or {
                "status": "completed",
                "stdout": "placeholder",
            },
            name="no_op",
            description="Return a placeholder",
        )
        probe = StructuredTool.from_function(
            lambda: calls.append("probe") or {
                "status": "completed",
                "stdout": "HTTP 403",
            },
            name="probe",
            description="Run the assigned verification",
        )
        llm = _SequencedLLM(
            [
                _ToolResponse(
                    "",
                    [{"name": "no_op", "args": {}, "id": "call-1"}],
                ),
                _Response(
                    'BLACKBOARD_RESULT: {"facts":[],"edges":[],'
                    '"goal_achieved":false,"final_answer":null}'
                ),
                _ToolResponse(
                    "",
                    [{"name": "probe", "args": {}, "id": "call-2"}],
                ),
                _Response(
                    'BLACKBOARD_RESULT: {"facts":[{"content":'
                    '"The assigned request returned HTTP 403."}],'
                    '"edges":[],"goal_achieved":false,'
                    '"final_answer":null}'
                ),
            ]
        )
        runner = ToolCallingAgentRunner(
            llm=llm,
            system_prompt="execution",
            tools=[no_op, probe],
            max_steps=4,
            require_tool_call_before_final=True,
            require_blackboard_progress_before_final=True,
        )

        result = await runner.arun("execute the Assigned Intent")

        self.assertIn("HTTP 403", result)
        self.assertEqual(calls, ["no-op", "probe"])
        correction_messages = llm.message_snapshots[2]
        self.assertTrue(
            any(
                "not a valid execution result" in str(message.content)
                for message in correction_messages
            )
        )

    async def test_repeated_tools_with_empty_results_reach_step_limit(
        self,
    ) -> None:
        calls: list[str] = []
        no_op = StructuredTool.from_function(
            lambda: calls.append("no-op") or {
                "status": "completed",
                "stdout": "placeholder",
            },
            name="no_op",
            description="Return a placeholder",
        )
        empty_result = (
            'BLACKBOARD_RESULT: {"facts":[],"edges":[],'
            '"goal_achieved":false,"final_answer":null}'
        )
        llm = _SequencedLLM(
            [
                _ToolResponse(
                    "",
                    [{"name": "no_op", "args": {}, "id": "call-1"}],
                ),
                _Response(empty_result),
                _ToolResponse(
                    "",
                    [{"name": "no_op", "args": {}, "id": "call-2"}],
                ),
                _Response(empty_result),
            ]
        )
        runner = ToolCallingAgentRunner(
            llm=llm,
            system_prompt="execution",
            tools=[no_op],
            max_steps=4,
            require_tool_call_before_final=True,
            require_blackboard_progress_before_final=True,
        )

        with self.assertRaisesRegex(RuntimeError, "maximum step count"):
            await runner.arun("execute the Assigned Intent")

        self.assertEqual(calls, ["no-op", "no-op"])
        self.assertEqual(llm.calls, 4)

    async def test_negative_observation_is_valid_execution_progress(self) -> None:
        probe = StructuredTool.from_function(
            lambda: {"status": "completed", "stdout": "connection refused"},
            name="probe",
            description="Run the assigned verification",
        )
        expected = (
            'BLACKBOARD_RESULT: {"facts":[{"content":'
            '"The target refused the connection."}],"edges":[],'
            '"goal_achieved":false,"final_answer":null}'
        )
        llm = _SequencedLLM(
            [
                _ToolResponse(
                    "",
                    [{"name": "probe", "args": {}, "id": "call-1"}],
                ),
                _Response(expected),
            ]
        )
        runner = ToolCallingAgentRunner(
            llm=llm,
            system_prompt="execution",
            tools=[probe],
            max_steps=2,
            require_tool_call_before_final=True,
            require_blackboard_progress_before_final=True,
        )

        result = await runner.arun("execute the Assigned Intent")

        self.assertEqual(result, expected)
        self.assertEqual(llm.calls, 2)

    async def test_timeout_request_runs_without_tools(self) -> None:
        llm = _FakeLLM("BLACKBOARD_RESULT: {\"intent_id\":\"i\"}")
        result = await run_request(TIMEOUT_PREFIX + "\nbody", llm=llm)
        self.assertIn("BLACKBOARD_RESULT:", result)
        self.assertEqual(llm.calls, 1)

    async def test_execution_request_dispatches_to_tool_runtime(self) -> None:
        llm = _FakeLLM("unused")
        with patch(
            "agent._run_with_tools",
            new=AsyncMock(return_value="BLACKBOARD_RESULT: {}"),
        ) as run_with_tools:
            result = await run_request(EXECUTION_PREFIX + "\nbody", llm=llm)

        self.assertEqual(result, "BLACKBOARD_RESULT: {}")
        run_with_tools.assert_awaited_once_with(
            EXECUTION_PREFIX + "\nbody",
            llm,
            None,
            None,
        )

    def test_execution_runtime_dependencies_import_together(self) -> None:
        dependencies = _load_execution_runtime()

        self.assertEqual(len(dependencies), 7)
        self.assertTrue(all(dependency is not None for dependency in dependencies))

    async def test_empty_request_is_rejected_before_llm_creation(self) -> None:
        with patch("agent._build_llm") as build_llm:
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                await run_request("  ")
        build_llm.assert_not_called()

    def test_skill_path_is_resolved_from_package_root(self) -> None:
        expected = PROJECT_ROOT / "Skills" / "sample" / "SKILL.md"
        factory = SkillAgentFactory(llm=_FakeLLM("unused"))

        with (
            patch.object(Path, "is_file", autospec=True, return_value=True) as is_file,
            patch.object(
                Path,
                "read_text",
                autospec=True,
                return_value="system prompt",
            ) as read_text,
        ):
            result = factory.load_skill_md("sample")

        self.assertEqual(result, "system prompt")
        is_file.assert_called_once_with(expected)
        read_text.assert_called_once_with(expected, encoding="utf-8")

    def test_skill_path_rejects_directory_traversal(self) -> None:
        factory = SkillAgentFactory(llm=_FakeLLM("unused"))
        with self.assertRaisesRegex(ValueError, "directory name"):
            factory.load_skill_md("../outside")

    def test_optional_allowlist_and_path_arguments(self) -> None:
        with patch.dict(
            "os.environ",
            {"QINYUAN_MCP_SERVERS": " nmap, httpx, nmap "},
            clear=True,
        ):
            self.assertEqual(
                _optional_csv_environment("QINYUAN_MCP_SERVERS"),
                frozenset({"nmap", "httpx"}),
            )
        self.assertIsNone(_optional_path_argument(""))
        self.assertEqual(_optional_path_argument("work"), Path("work"))

    def test_execution_identity_is_stable_for_work_directory(self) -> None:
        work_dir = Path(__file__).resolve().parent
        self.assertEqual(
            _execution_identity(work_dir),
            _execution_identity(work_dir),
        )

    def test_stages_only_referenced_sibling_execution_logs(self) -> None:
        root = Path(__file__).resolve().parent / ".stage-work"
        executions = root / "executions"
        current = executions / "run-qinyuan-agent-current"
        previous = executions / "run-qinyuan-agent-808eea68911d4ef1b3853adcb2810579"
        try:
            current.mkdir(parents=True)
            previous.mkdir()
            (previous / "stdout.txt").write_text("previous stdout\n", encoding="utf-8")
            (previous / "stderr.txt").write_text("previous stderr\n", encoding="utf-8")
            staged = _stage_requested_execution_logs(
                f"read {previous.name}/stdout.txt and {previous.name}/stderr.txt",
                current,
            )

            self.assertEqual(staged, (previous.name,))
            staged_root = current / "_previous_runs" / previous.name
            self.assertEqual(
                (staged_root / "stdout.txt").read_text(encoding="utf-8"),
                "previous stdout\n",
            )
            self.assertEqual(
                (staged_root / "stderr.txt").read_text(encoding="utf-8"),
                "previous stderr\n",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_provider_output_normalizes_surrogate_characters(self) -> None:
        output = _normalize_provider_output("valid \ud83d\ude00 invalid \udcbd")

        self.assertEqual(output, "valid \U0001f600 invalid \ufffd")
        self.assertEqual(
            output.encode("utf-8").decode("utf-8"),
            output,
        )

    def test_main_writes_surrogate_response_as_valid_utf8(self) -> None:
        raw_output = io.BytesIO()
        stdout = io.TextIOWrapper(raw_output, encoding="gbk", errors="strict")
        response = "BLACKBOARD_RESULT: {\"content\":\"中文\udcbd\"}"

        with (
            patch.object(sys, "stdin", io.StringIO("request")),
            patch.object(sys, "stdout", stdout),
            patch(
                "agent._run_request_with_signal_handlers",
                new=AsyncMock(return_value=response),
            ),
        ):
            status = main([])
            stdout.flush()

        self.assertEqual(status, 0)
        self.assertEqual(
            raw_output.getvalue().decode("utf-8").splitlines(),
            ["BLACKBOARD_RESULT: {\"content\":\"中文\ufffd\"}"],
        )

    def test_main_protocol_gate_retries_once_without_leaking_invalid_stdout(self) -> None:
        invalid = "BLACKBOARD_RESULT: {}"
        correction = self._valid_v2_result()
        stdout = io.StringIO()
        with (
            patch.object(sys, "stdin", io.StringIO(self._execution_request())),
            patch.object(sys, "stdout", stdout),
            patch(
                "agent._run_request_with_signal_handlers",
                new=AsyncMock(return_value=invalid),
            ),
            patch(
                "agent._run_protocol_correction",
                new=AsyncMock(return_value=correction),
            ) as retry,
        ):
            status = main(
                [
                    "--result-protocol-version",
                    "2",
                    "--required-skill",
                    "blackboard-result-protocol",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), correction + "\n")
        retry.assert_awaited_once()

    def test_main_protocol_gate_keeps_stdout_empty_after_second_failure(self) -> None:
        with (
            patch.object(sys, "stdin", io.StringIO(self._execution_request())),
            patch.object(sys, "stdout", io.StringIO()) as stdout,
            patch(
                "agent._run_request_with_signal_handlers",
                new=AsyncMock(return_value="BLACKBOARD_RESULT: {}"),
            ),
            patch(
                "agent._run_protocol_correction",
                new=AsyncMock(return_value="BLACKBOARD_RESULT: {}"),
            ),
        ):
            status = main(["--result-protocol-version", "2"])

        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")

    async def test_termination_signal_cancels_active_request(self) -> None:
        handlers = {}

        async def wait_for_cancellation(*_args, **_kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.sleep(0)

        with (
            patch("agent.run_request", side_effect=wait_for_cancellation),
            patch("agent.signal.getsignal", return_value=signal.SIG_DFL),
            patch(
                "agent.signal.signal",
                side_effect=lambda sig, handler: handlers.__setitem__(sig, handler),
            ),
        ):
            with self.assertRaises(AgentInterrupted):
                await _run_request_with_signal_handlers(
                    "request",
                    challenge_dir=None,
                    work_dir=None,
                )

    async def test_provider_cancel_file_cancels_active_request(self) -> None:
        work_dir = Path(__file__).resolve().parent / ".cancel-work"
        cancel_file = work_dir / ".provider-cancel-test"
        work_dir.mkdir(exist_ok=True)

        async def wait_for_cancellation(*_args, **_kwargs):
            await asyncio.Event().wait()

        environment = {"QINYUAN_PROVIDER_CANCEL_FILE": str(cancel_file)}
        try:
            with (
                patch.dict("os.environ", environment, clear=False),
                patch("agent.run_request", side_effect=wait_for_cancellation),
                patch("agent.signal.getsignal", return_value=signal.SIG_DFL),
                patch("agent.signal.signal"),
            ):
                task = asyncio.create_task(
                    _run_request_with_signal_handlers(
                        "request",
                        challenge_dir=None,
                        work_dir=work_dir,
                    )
                )
                await asyncio.sleep(0.1)
                cancel_file.write_text("cancel\n", encoding="utf-8")
                with self.assertRaises(AgentInterrupted):
                    await asyncio.wait_for(task, timeout=1)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
