from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


SKILLS_DIR = Path(__file__).resolve().parent
CONTENT_MAX_BYTES = 256 * 1024
RESOURCE_MAX_BYTES = 2 * 1024 * 1024


class DuplicateSkillError(ValueError):
    pass


def configure_limits(*, content_max_bytes: int, resource_max_bytes: int) -> None:
    global CONTENT_MAX_BYTES, RESOURCE_MAX_BYTES
    CONTENT_MAX_BYTES = content_max_bytes
    RESOURCE_MAX_BYTES = resource_max_bytes


def _parse_skill_file(skill_file: Path) -> dict[str, Any]:
    size = skill_file.stat().st_size
    if size > CONTENT_MAX_BYTES:
        raise ValueError(
            f"Skill file exceeds {CONTENT_MAX_BYTES} bytes: {skill_file}"
        )
    content = skill_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    metadata: dict[str, Any] = {}

    if lines and lines[0].strip() == "---":
        try:
            end_index = next(
                index
                for index, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError(
                f"Skill file {skill_file} has invalid YAML front matter"
            ) from exc
        parsed = yaml.safe_load("\n".join(lines[1:end_index])) or {}
        if not isinstance(parsed, dict):
            raise ValueError(f"Skill metadata must be a mapping: {skill_file}")
        metadata = parsed

    return {
        "name": str(metadata.get("name") or skill_file.parent.name).strip(),
        "description": str(metadata.get("description") or "").strip(),
        "content": content,
        "directory": skill_file.parent,
    }


def _discover() -> tuple[dict[str, dict[str, Any]], list[str]]:
    skills: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    duplicates: dict[str, list[Path]] = {}

    for skill_file in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            skill = _parse_skill_file(skill_file)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{skill_file.parent.name}: {exc}")
            continue
        name = skill["name"]
        if not name:
            errors.append(f"{skill_file.parent.name}: Skill name is empty")
            continue
        if name in skills:
            duplicates.setdefault(name, [skills[name]["directory"]]).append(
                skill_file.parent
            )
            continue
        skills[name] = skill

    if duplicates:
        details = "; ".join(
            f"{name}: {', '.join(str(path) for path in paths)}"
            for name, paths in duplicates.items()
        )
        raise DuplicateSkillError(f"Duplicate Skill names: {details}")
    return skills, errors


def discover_skills() -> dict[str, dict[str, Any]]:
    skills, _ = _discover()
    return skills


def build_skill_catalog() -> str:
    skills, errors = _discover()
    lines = [
        f"- {skill['name']}: {skill['description']}"
        for skill in skills.values()
    ]
    lines.extend(f"- [invalid] {error}" for error in errors)
    return "\n".join(lines) if lines else "No skills found."


def load_skill(name: str) -> str:
    skills, errors = _discover()
    skill = skills.get(name)
    if skill is None:
        available = ", ".join(skills) or "No skills available"
        error_text = f" Invalid skills: {'; '.join(errors)}" if errors else ""
        return f"Skill '{name}' not found. Available skills: {available}.{error_text}"
    return str(skill["content"])


def load_skill_resource(name: str, resource: str) -> str:
    skills, _ = _discover()
    skill = skills.get(name)
    if skill is None:
        raise KeyError(f"Skill not found: {name}")
    directory = Path(skill["directory"]).resolve()
    path = (directory / resource).resolve()
    try:
        path.relative_to(directory)
    except ValueError as exc:
        raise ValueError("Skill resource path escapes its directory") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Skill resource not found: {resource}")
    if path.stat().st_size > RESOURCE_MAX_BYTES:
        raise ValueError(
            f"Skill resource exceeds {RESOURCE_MAX_BYTES} bytes: {resource}"
        )
    return path.read_text(encoding="utf-8")
