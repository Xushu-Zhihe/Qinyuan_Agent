from __future__ import annotations

import asyncio
import shutil
import signal
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from Agents.agent import PROJECT_ROOT, SkillAgentFactory
from agent import (
    EXECUTION_PREFIX,
    PLANNING_PREFIX,
    TIMEOUT_PREFIX,
    AgentInterrupted,
    _execution_identity,
    _optional_csv_environment,
    _optional_path_argument,
    _run_request_with_signal_handlers,
    classify_request,
    run_request,
)


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


class AgentCliTests(unittest.IsolatedAsyncioTestCase):
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
