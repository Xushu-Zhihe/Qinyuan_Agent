from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "MCPs" / "Shell_Mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("qinyuan_test_shell_mcp", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load Shell MCP module")
shell = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shell
SPEC.loader.exec_module(shell)


class ShellMcpPlannerTests(unittest.TestCase):
    def test_bash_plan_runs_noninteractive_command(self) -> None:
        plan = shell.run_bash("curl -fsS http://example.test/")
        command = plan["sandbox_command"]

        self.assertEqual(command["tool"], "bash")
        self.assertEqual(
            command["arguments"][:3],
            ["--noprofile", "--norc", "-lc"],
        )
        self.assertEqual(command["arguments"][-1], "curl -fsS http://example.test/")

    def test_powershell_plan_is_noninteractive(self) -> None:
        plan = shell.run_powershell("Invoke-WebRequest http://example.test/")
        command = plan["sandbox_command"]

        self.assertEqual(command["tool"], "pwsh")
        self.assertEqual(
            command["arguments"][:3],
            ["-NoLogo", "-NoProfile", "-NonInteractive"],
        )
        self.assertEqual(command["arguments"][-2], "-Command")

    def test_invalid_script_and_timeout_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shell.run_bash("\0")
        with self.assertRaises(ValueError):
            shell.run_powershell("Write-Output ok", timeout_seconds=301)


if __name__ == "__main__":
    unittest.main()
