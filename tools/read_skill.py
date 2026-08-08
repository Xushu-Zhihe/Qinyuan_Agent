from __future__ import annotations

from skills.loader import build_skill_catalog, load_skill, load_skill_resource

from .registry import registry


def list_skills() -> str:
    return build_skill_catalog()


def read_skill(name: str) -> str:
    return load_skill(name)


def read_skill_resource(name: str, resource: str) -> str:
    return load_skill_resource(name, resource)


LIST_SKILLS_SCHEMA = {
    "type": "function",
    "name": "list_skills",
    "description": (
        "Discover local Skills and return their names, descriptions, and "
        "isolated validation errors."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}

READ_SKILL_SCHEMA = {
    "type": "function",
    "name": "read_skill",
    "description": "Read one discovered local SKILL.md by its exact name.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "strict": True,
}

READ_SKILL_RESOURCE_SCHEMA = {
    "type": "function",
    "name": "read_skill_resource",
    "description": "Read a bounded UTF-8 resource inside a discovered Skill.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "resource": {"type": "string"},
        },
        "required": ["name", "resource"],
        "additionalProperties": False,
    },
    "strict": True,
}

registry.register(
    "list_skills",
    LIST_SKILLS_SCHEMA,
    list_skills,
    read_only=True,
)
registry.register(
    "read_skill",
    READ_SKILL_SCHEMA,
    read_skill,
    read_only=True,
)
registry.register(
    "read_skill_resource",
    READ_SKILL_RESOURCE_SCHEMA,
    read_skill_resource,
    read_only=True,
)
