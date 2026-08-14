from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from MCPs.Mcp_client.client import (
    MCPClientError,
    MCPManager,
    MCPRegistryConfig,
    MCPServerConfig,
    MCPServerRuntime,
    MCPToolMeta,
)
from Sandbox.models import CommandResult


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test module {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


nmap = _load_module("qinyuan_test_nmap", "MCPs/Nmap_Mcp/server.py")
subfinder = _load_module("qinyuan_test_subfinder", "MCPs/Subfinder_Mcp/server.py")
amass = _load_module("qinyuan_test_amass", "MCPs/Amass_Mcp/server.py")
httpx = _load_module("qinyuan_test_httpx", "MCPs/Httpx_Mcp/server.py")
ffuf = _load_module("qinyuan_test_ffuf", "MCPs/ffuf_mcp/server.py")
dirsearch = _load_module(
    "qinyuan_test_dirsearch",
    "MCPs/Dirsearch_Mcp/src/server.py",
)
web = _load_module("qinyuan_test_web", "MCPs/Web_Mcp/server.py")

NUCLEI_ROOT = ROOT / "MCPs" / "Nuclei_mcp"
sys.path.insert(0, str(NUCLEI_ROOT))
try:
    from nuclei_mcp import security as nuclei_security
finally:
    sys.path.remove(str(NUCLEI_ROOT))


class EnabledMcpPolicyTests(unittest.TestCase):
    def test_every_enabled_server_rejects_an_unauthorized_target(self) -> None:
        checks = {
            "nmap": lambda: nmap._validate_target("example.com"),
            "subfinder": lambda: subfinder._validate_domain("example.com"),
            "amass": lambda: amass._validate_domain("example.com"),
            "httpx": lambda: httpx._validate_target("https://example.com"),
            "ffuf": lambda: ffuf._validate_target("https://example.com/FUZZ"),
            "dirsearch": lambda: dirsearch._validate_target(
                "https://example.com"
            ),
            "nuclei": lambda: nuclei_security.validate_target(
                "https://example.com"
            ),
            "web": lambda: web._validate_url("https://example.com"),
        }

        for server_name, check in checks.items():
            with self.subTest(server=server_name):
                with self.assertRaises(PermissionError):
                    check()

    def test_every_enabled_server_rejects_an_excessive_timeout(self) -> None:
        checks = {
            "nmap": lambda: nmap._validate_timeout(301),
            "subfinder": lambda: subfinder._validate_timeout(301),
            "amass": lambda: amass._validate_timeout(301),
            "httpx": lambda: httpx._validate_timeout(301),
            "ffuf": lambda: ffuf._validate_timeout(301),
            "dirsearch": lambda: dirsearch._validate_int(
                301,
                "timeout_seconds",
                5,
                300,
            ),
            "nuclei": lambda: nuclei_security.validate_timeout(301),
            "web": lambda: web._validate_timeout(301),
        }

        for server_name, check in checks.items():
            with self.subTest(server=server_name):
                with self.assertRaises(ValueError):
                    check()

    def test_path_accepting_servers_reject_unapproved_or_traversing_paths(
        self,
    ) -> None:
        checks = {
            "ffuf": lambda: ffuf._validate_wordlist("../../etc/passwd"),
            "dirsearch": lambda: dirsearch._validate_wordlist(
                "../../etc/passwd"
            ),
            "nuclei": lambda: nuclei_security.validate_template(
                "../../private.yaml"
            ),
        }

        for server_name, check in checks.items():
            with self.subTest(server=server_name):
                with self.assertRaises(ValueError):
                    check()

    def test_manager_truncates_large_tool_output_at_both_ends(self) -> None:
        manager = MCPManager(
            MCPRegistryConfig({}),
            object(),
            output_character_limit=1_000,
        )
        output = "A" * 700 + "B" * 700

        rendered, truncated = manager._truncate_output(output)

        self.assertTrue(truncated)
        self.assertTrue(rendered.startswith("A" * 500))
        self.assertTrue(rendered.endswith("B" * 500))
        self.assertIn("400 characters omitted", rendered)


class _SlowSession:
    async def call_tool(self, *_args, **_kwargs):
        await asyncio.sleep(1)


class _SuccessfulSession:
    async def call_tool(self, *_args, **_kwargs):
        plan = {
            "sandbox_command": {
                "tool": "curl",
                "arguments": ["--url", "http://target-web/"],
                "timeout_seconds": 10,
            }
        }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(plan))],
            structuredContent=None,
            isError=False,
        )


class _SuccessfulSandbox:
    def execute(self, agent_id, request):
        self.called = (agent_id, request)
        return CommandResult(
            sandbox_id="sandbox-1",
            exit_code=0,
            stdout="HTTP 200",
            stderr="",
        )


class McpManagerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_plan_timeout_stops_before_sandbox_execution(self) -> None:
        config = MCPServerConfig(
            name="slow",
            sandbox_tools=frozenset({"nmap"}),
        )
        manager = MCPManager(
            MCPRegistryConfig({"slow": config}),
            object(),
            mcp_call_timeout_seconds=0.01,
        )
        manager.runtimes["slow"] = MCPServerRuntime(
            config=config,
            session=_SlowSession(),
            status="ready",
        )
        manager.tool_index["slow.scan"] = MCPToolMeta(
            server_name="slow",
            tool_name="scan",
            qualified_name="slow.scan",
            description="",
            input_schema={},
            tags=frozenset(),
        )

        with self.assertRaisesRegex(MCPClientError, "command plan in time"):
            await manager._execute_bound_tool(
                agent_id="agent-1",
                qualified_name="slow.scan",
                arguments={},
            )

    async def test_successful_tool_call_writes_started_and_finished_audit(
        self,
    ) -> None:
        root = ROOT / ".test-work" / uuid4().hex
        root.mkdir(parents=True)
        audit_path = root / "mcp-audit.jsonl"
        sandbox = _SuccessfulSandbox()
        config = MCPServerConfig(
            name="web",
            sandbox_tools=frozenset({"curl"}),
        )
        manager = MCPManager(
            MCPRegistryConfig({"web": config}),
            sandbox,
            audit_path=audit_path,
        )
        manager.runtimes["web"] = MCPServerRuntime(
            config=config,
            session=_SuccessfulSession(),
            status="ready",
        )
        manager.tool_index["web.http_request"] = MCPToolMeta(
            server_name="web",
            tool_name="http_request",
            qualified_name="web.http_request",
            description="",
            input_schema={},
            tags=frozenset(),
        )
        try:
            result = await manager._execute_bound_tool(
                agent_id="agent-1",
                qualified_name="web.http_request",
                arguments={"url": "http://target-web/"},
            )
            records = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(result.status, "completed")
            self.assertEqual([record["event"] for record in records], [
                "started",
                "finished",
            ])
            self.assertEqual(records[1]["sandbox_id"], "sandbox-1")
            self.assertEqual(records[1]["tool"], "web.http_request")
        finally:
            shutil.rmtree(root, ignore_errors=True)
            try:
                root.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
