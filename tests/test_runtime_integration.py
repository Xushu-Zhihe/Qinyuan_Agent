from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from MCPs.Mcp_client import build_mcp_runtime
from Sandbox.docker_runtime import DockerRuntime
from Sandbox.models import CommandRequest, SandboxMounts
from Sandbox.service import SandboxService
from Sandbox.settings import SandboxSettings


RUN_DOCKER_TESTS = os.environ.get("QINYUAN_RUN_DOCKER_TESTS") == "1"
RUN_AUTHORIZED_TARGET_TESTS = (
    os.environ.get("QINYUAN_RUN_AUTHORIZED_TARGET_TESTS") == "1"
)
AUTHORIZED_TARGET_URL = os.environ.get("QINYUAN_AUTHORIZED_TARGET_URL", "")


@unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
class DockerRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_mcp_tool_executes_in_dedicated_sandbox(self) -> None:
        agent_id = "integration-mcp"
        service = SandboxService(await asyncio.to_thread(DockerRuntime))
        manager = None

        try:
            session = await asyncio.to_thread(service.start_for_agent, agent_id)
            manager = await build_mcp_runtime(service)
            qualified_names = {
                tool.qualified_name for tool in manager.list_registered_tools()
            }
            self.assertIn("nmap.run_nmap_tcp_scan", qualified_names)

            client = manager.bind_agent(
                agent_id,
                allowed_tools={"nmap.run_nmap_tcp_scan"},
            )
            result = await client.call_tool(
                "nmap.run_nmap_tcp_scan",
                {
                    "target": "127.0.0.1",
                    "ports": "80",
                    "timeout_seconds": 10,
                },
            )

            self.assertEqual(result.sandbox_id, session.sandbox_id)
            self.assertEqual(result.qualified_name, "nmap.run_nmap_tcp_scan")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Nmap scan report", result.stdout)
        finally:
            if manager is not None:
                await manager.stop_all()
            await asyncio.to_thread(service.close)

    async def test_challenge_is_readonly_and_work_is_writable(self) -> None:
        root = Path(__file__).resolve().parent / ".runtime-work" / uuid4().hex
        challenge = root / "challenge"
        work = root / "work"
        challenge.mkdir(parents=True)
        work.mkdir()
        (challenge / "input.txt").write_text("verified", encoding="utf-8")

        settings = SandboxSettings(allowed_tools=frozenset({"python3"}))
        service = SandboxService(
            await asyncio.to_thread(DockerRuntime, settings),
            settings,
        )
        try:
            await asyncio.to_thread(
                service.start_for_agent,
                "integration-mounts",
                SandboxMounts(challenge, work),
            )
            result = await asyncio.to_thread(
                service.execute,
                "integration-mounts",
                CommandRequest(
                    tool="python3",
                    arguments=(
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('/challenge/input.txt').read_text() == 'verified'; "
                        "Path('/work/output.txt').write_text('writable')",
                    ),
                ),
            )
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual((work / "output.txt").read_text(), "writable")

            read_only = await asyncio.to_thread(
                service.execute,
                "integration-mounts",
                CommandRequest(
                    tool="python3",
                    arguments=(
                        "-c",
                        "from pathlib import Path; "
                        "Path('/challenge/blocked.txt').write_text('blocked')",
                    ),
                ),
            )
            self.assertNotEqual(read_only.exit_code, 0)
            self.assertFalse((challenge / "blocked.txt").exists())
        finally:
            await asyncio.to_thread(service.close)
            shutil.rmtree(root, ignore_errors=True)
            try:
                root.parent.rmdir()
            except OSError:
                pass

    async def test_runtime_enforces_timeout_and_output_byte_limit(self) -> None:
        settings = SandboxSettings(
            allowed_tools=frozenset({"python3"}),
            maximum_output_bytes_per_stream=1_024,
        )
        service = SandboxService(
            await asyncio.to_thread(DockerRuntime, settings),
            settings,
        )
        try:
            await asyncio.to_thread(service.start_for_agent, "integration-limits")
            timed_out = await asyncio.to_thread(
                service.execute,
                "integration-limits",
                CommandRequest(
                    tool="python3",
                    arguments=("-c", "import time; time.sleep(5)"),
                    timeout_seconds=1,
                ),
            )
            self.assertTrue(timed_out.timed_out)
            self.assertEqual(timed_out.exit_code, 124)

            oversized = await asyncio.to_thread(
                service.execute,
                "integration-limits",
                CommandRequest(
                    tool="python3",
                    arguments=("-c", "print('x' * 5000)"),
                    timeout_seconds=10,
                ),
            )
            self.assertEqual(oversized.exit_code, 0, oversized.stderr)
            self.assertTrue(oversized.stdout_truncated)
            self.assertIn("bytes omitted by Sandbox Runtime", oversized.stdout)
            self.assertLess(len(oversized.stdout), 1_200)
        finally:
            await asyncio.to_thread(service.close)

    async def test_shell_mcp_runs_bash_and_powershell_in_sandbox(self) -> None:
        root = Path(__file__).resolve().parent / ".runtime-work" / uuid4().hex
        work = root / "work"
        work.mkdir(parents=True)
        settings = SandboxSettings()
        service = SandboxService(
            await asyncio.to_thread(DockerRuntime, settings),
            settings,
        )
        manager = None
        try:
            await asyncio.to_thread(
                service.start_for_agent,
                "integration-shell",
                SandboxMounts(work_dir=work),
            )
            manager = await build_mcp_runtime(
                service,
                allowed_servers={"shell"},
                audit_path=work / "mcp-audit.jsonl",
            )
            client = manager.bind_agent(
                "integration-shell",
                allowed_tools={"shell.run_bash", "shell.run_powershell"},
            )

            bash_result = await client.call_tool(
                "shell.run_bash",
                {"script": "printf 'bash-ok\\n'"},
            )
            powershell_result = await client.call_tool(
                "shell.run_powershell",
                {"script": "Write-Output 'powershell-ok'"},
            )

            self.assertEqual(bash_result.status, "completed", bash_result.stderr)
            self.assertEqual(bash_result.stdout.strip(), "bash-ok")
            self.assertEqual(
                powershell_result.status,
                "completed",
                powershell_result.stderr,
            )
            self.assertEqual(powershell_result.stdout.strip(), "powershell-ok")
            records = [
                json.loads(line)
                for line in (work / "mcp-audit.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                {
                    record["tool"]
                    for record in records
                    if record["event"] == "finished"
                },
                {"shell.run_bash", "shell.run_powershell"},
            )
        finally:
            if manager is not None:
                await manager.stop_all()
            await asyncio.to_thread(service.close)
            shutil.rmtree(root, ignore_errors=True)
            try:
                root.parent.rmdir()
            except OSError:
                pass

    async def test_image_contains_all_registered_runtime_tools(self) -> None:
        settings = SandboxSettings(
            allowed_tools=frozenset({"python3", "pwsh"})
        )
        service = SandboxService(
            await asyncio.to_thread(DockerRuntime, settings),
            settings,
        )
        try:
            await asyncio.to_thread(service.start_for_agent, "integration-tools")
            result = await asyncio.to_thread(
                service.execute,
                "integration-tools",
                CommandRequest(
                    tool="python3",
                    arguments=(
                        "-c",
                        "import shutil,sys; "
                        "tools=['bash','nmap','curl','subfinder','amass','httpx',"
                        "'ffuf','dirsearch','nuclei','pwsh']; "
                        "missing=[tool for tool in tools if not shutil.which(tool)]; "
                        "print(','.join(missing)); sys.exit(bool(missing))",
                    ),
                ),
            )
            self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            powershell = await asyncio.to_thread(
                service.execute,
                "integration-tools",
                CommandRequest(
                    tool="pwsh",
                    arguments=(
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "$PSVersionTable.PSVersion.ToString()",
                    ),
                ),
            )
            self.assertEqual(
                powershell.exit_code,
                0,
                powershell.stdout + powershell.stderr,
            )
            self.assertTrue(powershell.stdout.strip().startswith("7.5."))
        finally:
            await asyncio.to_thread(service.close)

    @unittest.skipUnless(
        RUN_AUTHORIZED_TARGET_TESTS and bool(AUTHORIZED_TARGET_URL),
        "set QINYUAN_RUN_AUTHORIZED_TARGET_TESTS=1 and target URL",
    )
    async def test_curl_and_powershell_reach_authorized_target_in_sandbox(
        self,
    ) -> None:
        root = Path(__file__).resolve().parent / ".runtime-work" / uuid4().hex
        work = root / "work"
        work.mkdir(parents=True)
        settings = SandboxSettings.from_environment()
        self.assertFalse(settings.network_internal)
        service = SandboxService(
            await asyncio.to_thread(DockerRuntime, settings),
            settings,
        )
        manager = None
        try:
            await asyncio.to_thread(
                service.start_for_agent,
                "integration-authorized-shell",
                SandboxMounts(work_dir=work),
            )
            manager = await build_mcp_runtime(
                service,
                allowed_servers={"shell"},
                audit_path=work / "mcp-audit.jsonl",
            )
            client = manager.bind_agent(
                "integration-authorized-shell",
                allowed_tools={
                    "shell.run_bash",
                    "shell.run_powershell",
                },
            )
            curl_result = await client.call_tool(
                "shell.run_bash",
                {
                    "script": (
                        "curl --silent --show-error --include --max-time 30 "
                        f"--url {shlex.quote(AUTHORIZED_TARGET_URL)}"
                    ),
                    "timeout_seconds": 35,
                },
            )
            powershell_target = AUTHORIZED_TARGET_URL.replace("'", "''")
            powershell_result = await client.call_tool(
                "shell.run_powershell",
                {
                    "script": (
                        "$response = Invoke-WebRequest "
                        f"-Uri '{powershell_target}' -UseBasicParsing "
                        "-SkipHttpErrorCheck -TimeoutSec 30; "
                        "Write-Output ('HTTP status: {0}' -f "
                        "[int]$response.StatusCode); Write-Output $response.Content"
                    ),
                    "timeout_seconds": 35,
                },
            )
            self.assertEqual(
                curl_result.status,
                "completed",
                curl_result.stderr,
            )
            self.assertIn("HTTP/", curl_result.stdout)
            self.assertEqual(
                powershell_result.status,
                "completed",
                powershell_result.stderr,
            )
            self.assertIn("HTTP status:", powershell_result.stdout)
            records = [
                json.loads(line)
                for line in (work / "mcp-audit.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            finished_tools = {
                record["tool"]
                for record in records
                if record["event"] == "finished"
            }
            self.assertEqual(
                finished_tools,
                {"shell.run_bash", "shell.run_powershell"},
            )
        finally:
            if manager is not None:
                await manager.stop_all()
            await asyncio.to_thread(service.close)
            shutil.rmtree(root, ignore_errors=True)
            try:
                root.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
