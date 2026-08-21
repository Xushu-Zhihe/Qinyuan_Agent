from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test module {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


file_mcp = _load_module("qinyuan_test_file_mcp", "MCPs/File_Mcp/server.py")
jadx_mcp = _load_module("qinyuan_test_jadx_mcp", "MCPs/Jadx_Mcp/server.py")
binutils_mcp = _load_module(
    "qinyuan_test_binutils_mcp", "MCPs/Binutils_Mcp/server.py"
)
gdb_mcp = _load_module("qinyuan_test_gdb_mcp", "MCPs/Gdb_Mcp/server.py")
pwntools_mcp = _load_module(
    "qinyuan_test_pwntools_mcp", "MCPs/Pwntools_Mcp/server.py"
)
z3_mcp = _load_module("qinyuan_test_z3_mcp", "MCPs/Z3_Mcp/server.py")
crypto_mcp = _load_module(
    "qinyuan_test_crypto_mcp", "MCPs/Pycryptodome_Mcp/server.py"
)


class AnalysisMcpPlannerTests(unittest.TestCase):
    def test_registry_connects_all_analysis_servers_to_expected_executables(self) -> None:
        from MCPs.Mcp_client.client import load_registry

        registry = load_registry(ROOT / "MCPs" / "Mcp_client" / "registry.json")
        expected = {
            "file": {"file"},
            "jadx": {"jadx"},
            "binutils": {"readelf", "objdump", "nm", "strings"},
            "gdb": {"gdb"},
            "pwntools": {"python3"},
            "z3": {"python3"},
            "pycryptodome": {"python3"},
        }
        for name, tools in expected.items():
            with self.subTest(server=name):
                self.assertTrue(registry.servers[name].enabled)
                self.assertEqual(registry.servers[name].sandbox_tools, tools)

    def test_file_and_binutils_plans_use_sandbox_paths(self) -> None:
        file_plan = file_mcp.inspect_file("/challenge/sample.bin")
        self.assertEqual(file_plan["sandbox_command"]["tool"], "file")
        self.assertIn("/challenge/sample.bin", file_plan["sandbox_command"]["arguments"])

        elf_plan = binutils_mcp.inspect_binary(
            "/work/sample.bin", operation="disassemble"
        )
        self.assertEqual(elf_plan["sandbox_command"]["tool"], "objdump")
        self.assertIn("--", elf_plan["sandbox_command"]["arguments"])

        nm_plan = binutils_mcp.inspect_binary("/work/sample.bin", operation="nm")
        self.assertEqual(nm_plan["sandbox_command"]["tool"], "nm")

    def test_jadx_plan_decompiles_into_work_directory(self) -> None:
        plan = jadx_mcp.decompile_android(
            "/challenge/application.apk",
            "/work/jadx/application",
        )
        command = plan["sandbox_command"]

        self.assertEqual(command["tool"], "jadx")
        self.assertEqual(
            command["arguments"],
            [
                "--output-dir",
                "/work/jadx/application",
                "--deobf",
                "/challenge/application.apk",
            ],
        )
        self.assertEqual(command["timeout_seconds"], 300)

    def test_jadx_plan_rejects_unsafe_input_and_output_paths(self) -> None:
        invalid_cases = [
            ("/etc/passwd", "/work/jadx-output"),
            ("/challenge/application.apk", "/challenge/output"),
            ("/challenge/application.apk", "/work"),
            ("/challenge/application.apk", "/work/../challenge/output"),
        ]
        for input_path, output_dir in invalid_cases:
            with self.subTest(input_path=input_path, output_dir=output_dir):
                with self.assertRaises(ValueError):
                    jadx_mcp.decompile_android(input_path, output_dir)

        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            jadx_mcp.decompile_android(
                "/challenge/application.apk",
                deobfuscate="yes",
            )

    def test_docker_image_installs_pinned_jadx_release(self) -> None:
        dockerfile = (ROOT / "Sandbox" / "dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG JADX_VERSION=1.5.6", dockerfile)
        self.assertIn(
            "545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974",
            dockerfile,
        )
        self.assertIn("openjdk-17-jre-headless", dockerfile)
        self.assertIn("command -v jadx", dockerfile)

    def test_gdb_plan_is_batch_and_rejects_escape_commands(self) -> None:
        plan = gdb_mcp.debug_binary(
            "/challenge/sample.bin",
            commands=["set pagination off", "info functions"],
        )
        command = plan["sandbox_command"]
        self.assertEqual(command["tool"], "gdb")
        self.assertIn("--batch", command["arguments"])
        self.assertIn("-ex", command["arguments"])

        for unsafe in ["shell id", "source /tmp/init.gdb", "python print(1)"]:
            with self.subTest(command=unsafe):
                with self.assertRaises(ValueError):
                    gdb_mcp.debug_binary("/challenge/sample.bin", [unsafe])

    def test_python_analysis_plans_use_python3_and_validate_script_size(self) -> None:
        cases = [
            (pwntools_mcp.run_pwntools, "from pwn import *\nprint('ok')"),
            (z3_mcp.solve_constraints, "from z3 import *\nprint(sat)"),
            (crypto_mcp.run_crypto, "from Crypto.Util.number import long_to_bytes\nprint('ok')"),
        ]
        for planner, script in cases:
            with self.subTest(planner=planner.__name__):
                plan = planner(script)
                self.assertEqual(plan["sandbox_command"]["tool"], "python3")
                self.assertEqual(plan["sandbox_command"]["arguments"][0], "-c")
                self.assertEqual(plan["sandbox_command"]["arguments"][1], script)

        with self.assertRaises(ValueError):
            pwntools_mcp.run_pwntools("x" * (64 * 1024 + 1))

    def test_analysis_paths_and_operations_reject_unsafe_values(self) -> None:
        with self.assertRaises(ValueError):
            file_mcp.inspect_file("/etc/passwd")
        with self.assertRaises(ValueError):
            binutils_mcp.inspect_binary("/challenge/../work/sample.bin")
        with self.assertRaises(ValueError):
            binutils_mcp.inspect_binary("/challenge/sample.bin", operation="unknown")
        with self.assertRaises(ValueError):
            z3_mcp.solve_constraints("print('ok')", timeout_seconds=301)


if __name__ == "__main__":
    unittest.main()
