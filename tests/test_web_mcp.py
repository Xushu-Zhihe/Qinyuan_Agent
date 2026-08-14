from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "MCPs" / "Web_Mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("qinyuan_test_web_mcp", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load Web MCP module")
web = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = web
SPEC.loader.exec_module(web)


class WebMcpPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"MCP_WEB_ALLOWED_HOSTS": "node1.anna.nssctf.cn"},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_curl_plan_is_bounded_and_preserves_url_as_one_argument(self) -> None:
        target = "http://node1.anna.nssctf.cn:23635/?id=1"
        plan = web.http_request(target, headers={"Accept": "text/html"})
        command = plan["sandbox_command"]

        self.assertEqual(command["tool"], "curl")
        self.assertEqual(command["arguments"][-2:], ["--url", target])
        self.assertIn("--max-time", command["arguments"])
        self.assertLessEqual(command["timeout_seconds"], 125)

    def test_powershell_plan_uses_fixed_noninteractive_script(self) -> None:
        target = "http://node1.anna.nssctf.cn:23635/?id=1"
        plan = web.powershell_http_request(target)
        command = plan["sandbox_command"]

        self.assertEqual(command["tool"], "pwsh")
        self.assertEqual(command["arguments"][:3], [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
        ])
        self.assertEqual(command["arguments"][-2], "-EncodedCommand")
        script = base64.b64decode(command["arguments"][-1]).decode("utf-16-le")
        self.assertIn("Invoke-WebRequest", script)
        match = re.search(r"FromBase64String\('([^']+)'\)", script)
        self.assertIsNotNone(match)
        assert match is not None
        payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
        self.assertEqual(payload["url"], target)

    def test_unauthorized_host_and_header_injection_are_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            web.http_request("https://example.com/")
        with self.assertRaises(ValueError):
            web.http_request(
                "http://node1.anna.nssctf.cn:23635/",
                headers={"X-Test": "ok\r\nInjected: true"},
            )


if __name__ == "__main__":
    unittest.main()
