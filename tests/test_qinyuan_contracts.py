from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from qsubagent.config import (
    AppSettings,
    CapabilitySettings,
    ModelSettings,
    RuntimeSettings,
)
from qsubagent.mcp.manager import NmapMcpManager, build_fake_mcp_client
from qsubagent.mcp.planner import NmapPlanner, NmapPolicyError
from qsubagent.provider import run_provider_async
from qsubagent.runtime import ActiveTurn
from qsubagent.sandbox.contracts import (
    BridgeProtocolError,
    CommandRequest,
    CommandResult,
    RunContext,
)
from tools.nmap import NmapProxy
from tools.profiles import build_profile_registry, preflight_profile


def context(root: Path, profile: str = "nmap_optional") -> RunContext:
    artifact_scope = root / "artifacts"
    artifact_scope.mkdir(parents=True, exist_ok=True)
    return RunContext(
        protocol_version=1,
        agent_run_id="run-test",
        thread_id="thread-test",
        tool_profile=profile,
        authorized_targets=("127.0.0.1", "10.0.0.0/8"),
        deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        work_dir=str(root.resolve()),
        artifact_scope=str(artifact_scope.resolve()),
        result_contract={"prefix": "BLACKBOARD_RESULT:", "version": 1},
        sandbox_endpoint="http://127.0.0.1:12345",
    )


class FakeBridge:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []
        self.cancelled: list[str] = []

    async def execute(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(
            call_id=request.call_id,
            status="completed",
            exit_code=0,
            stdout="port 80 open",
            stderr="",
            artifact_refs=("artifact:run-test:call-1:stdout",),
        )

    async def cancel(self, call_id: str) -> bool:
        self.cancelled.append(call_id)
        return True


class FakeResponses:
    def __init__(self, text: str) -> None:
        self.text = text

    async def create(self, **_: object) -> object:
        return SimpleNamespace(
            id="response-1",
            output=[{"type": "message", "role": "assistant", "content": self.text}],
            output_text=self.text,
            usage=SimpleNamespace(input_tokens=10),
        )


class ContractTests(unittest.TestCase):
    def test_command_contract_is_strict_and_round_trips(self) -> None:
        request = CommandRequest.from_dict(
            {
                "protocol_version": 1,
                "call_id": "call-1",
                "tool": "nmap",
                "arguments": ["-sT", "127.0.0.1"],
                "timeout_seconds": 5,
            }
        )
        self.assertEqual(CommandRequest.from_dict(request.to_dict()), request)
        with self.assertRaises(BridgeProtocolError):
            CommandRequest.from_dict(request.to_dict() | {"sandbox_id": "other"})

    def test_profile_preflight_has_no_host_tools(self) -> None:
        self.assertEqual(preflight_profile("no_tools").tools, ())
        self.assertNotIn("powershell", preflight_profile("nmap_optional").tools)
        with self.assertRaises(ValueError):
            preflight_profile("unknown")

    def test_nmap_planner_authorizes_target_and_rejects_injection(self) -> None:
        planner = NmapPlanner(("127.0.0.1", "10.0.0.0/8"))
        plan = planner.plan(
            call_id="call-1", target="127.0.0.1", ports=[443, 80], timeout_seconds=10
        )
        self.assertEqual(plan.command.arguments, ("-sT", "-Pn", "-n", "-p", "80,443", "127.0.0.1"))
        with self.assertRaises(NmapPolicyError):
            planner.plan(call_id="call-2", target="192.0.2.1", ports=[80])
        with self.assertRaises(NmapPolicyError):
            planner.plan(call_id="call-3", target="127.0.0.1;echo bad", ports=[80])

    def test_manager_starts_lazily_and_proxy_calls_bridge(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_context = context(root)
                fake_mcp = build_fake_mcp_client(run_context)
                manager = NmapMcpManager(run_context, client=fake_mcp)
                bridge = FakeBridge()
                proxy = NmapProxy(manager, bridge)  # type: ignore[arg-type]
                self.assertFalse(manager.started)
                turn = ActiveTurn(running_call_id="call-1")
                result = await proxy.scan(
                    target="127.0.0.1", ports=[80], timeout_seconds=5, turn=turn
                )
                self.assertEqual(result.status, "completed")
                self.assertTrue(manager.started)
                self.assertEqual(len(bridge.requests), 1)
                self.assertEqual(bridge.requests[0].arguments[-1], "127.0.0.1")
                await manager.close()
                self.assertTrue(fake_mcp.closed)

        asyncio.run(scenario())


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_tools_provider_emits_one_blackboard_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_context = context(root, "no_tools")
            text = 'BLACKBOARD_RESULT: {"intent_id":"intent-1","facts":[],"edges":[],"goal_achieved":false,"answer_key":null,"final_answer":null}'
            settings = AppSettings(
                model=ModelSettings("test-model", "test-key", ""),
                capabilities=CapabilitySettings(previous_response_id=False, local_compaction=False),
                runtime=RuntimeSettings(state_dir=root / "unused"),
            )
            result = await run_provider_async(
                "complete this fake task",
                run_context,
                settings=settings,
                client=SimpleNamespace(responses=FakeResponses(text)),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.protocol_line, text)


if __name__ == "__main__":
    unittest.main()
