from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills import loader


class SkillLoaderTests(unittest.TestCase):
    def test_broken_skill_does_not_hide_valid_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good"
            bad = root / "bad"
            good.mkdir()
            bad.mkdir()
            (good / "SKILL.md").write_text(
                "---\nname: good\ndescription: works\n---\n# Good\n",
                encoding="utf-8",
            )
            (bad / "SKILL.md").write_text(
                "---\nname: [broken\n---\n",
                encoding="utf-8",
            )

            with patch.object(loader, "SKILLS_DIR", root):
                catalog = loader.build_skill_catalog()
                content = loader.load_skill("good")

            self.assertIn("good: works", catalog)
            self.assertIn("[invalid]", catalog)
            self.assertIn("# Good", content)

    def test_duplicate_skill_name_is_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("one", "two"):
                path = root / folder
                path.mkdir()
                (path / "SKILL.md").write_text(
                    "---\nname: duplicate\n---\n",
                    encoding="utf-8",
                )

            with patch.object(loader, "SKILLS_DIR", root):
                with self.assertRaisesRegex(
                    loader.DuplicateSkillError,
                    "Duplicate Skill names",
                ):
                    loader.discover_skills()

    def test_skill_content_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "large"
            path.mkdir()
            (path / "SKILL.md").write_text("x" * 100, encoding="utf-8")

            with (
                patch.object(loader, "SKILLS_DIR", root),
                patch.object(loader, "CONTENT_MAX_BYTES", 10),
            ):
                catalog = loader.build_skill_catalog()

            self.assertIn("[invalid]", catalog)

    def test_real_project_skill_is_discoverable(self) -> None:
        catalog = loader.build_skill_catalog()
        content = loader.load_skill("workspace-inspection")

        self.assertIn("workspace-inspection", catalog)
        self.assertIn("Get-ChildItem -Force", content)
