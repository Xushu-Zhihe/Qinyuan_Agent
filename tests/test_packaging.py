from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path
import tomllib
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
            self.config = tomllib.load(stream)

    def test_all_imported_local_packages_are_included(self) -> None:
        imported_roots: set[str] = set()
        for path in PROJECT_ROOT.rglob("*.py"):
            if any(part in {".git", ".venv", "build", "dist"} for part in path.parts):
                continue
            imported_roots.update(_imported_roots(path))

        local_packages = {
            path.name
            for path in PROJECT_ROOT.iterdir()
            if path.is_dir() and (path / "__init__.py").is_file()
        }
        required_packages = imported_roots.intersection(local_packages)
        include_patterns = self.config["tool"]["setuptools"]["packages"]["find"]["include"]
        missing = sorted(
            package
            for package in required_packages
            if not any(fnmatchcase(package, pattern) for pattern in include_patterns)
        )
        self.assertEqual(missing, [], f"local imports omitted from package discovery: {missing}")

    def test_runtime_resources_are_declared_as_package_data(self) -> None:
        package_data = self.config["tool"]["setuptools"]["package-data"]
        self.assertIn("**/*.json", package_data["Skills"])
        self.assertIn("**/*.md", package_data["Skills"])
        self.assertIn("registry.json", package_data["MCPs.Mcp_client"])

    def test_tool_registry_uses_the_actual_package_name(self) -> None:
        self.assertTrue((PROJECT_ROOT / "tools" / "__init__.py").is_file())
        imports = _imported_roots(PROJECT_ROOT / "agent.py")
        self.assertIn("tools", imports)
        self.assertNotIn("Tools", imports)


if __name__ == "__main__":
    unittest.main()
