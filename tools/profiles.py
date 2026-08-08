from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qsubagent.config import AppSettings
from qsubagent.mcp.manager import NmapMcpManager
from qsubagent.runtime import ArtifactStore
from qsubagent.sandbox.client import SandboxBridgeClient
from qsubagent.sandbox.contracts import RunContext

from .nmap import NMAP_SCHEMA, NmapProxy
from .read_artifact import READ_ARTIFACT_SCHEMA, read_artifact
from .read_skill import (
    LIST_SKILLS_SCHEMA,
    READ_SKILL_RESOURCE_SCHEMA,
    READ_SKILL_SCHEMA,
    list_skills,
    read_skill,
    read_skill_resource,
)
from .registry import ToolRegistry


PROFILE_VERSION = 1
PROFILE_NAMES = frozenset(
    {"interactive_unrestricted", "no_tools", "nmap_optional"}
)


@dataclass(frozen=True, slots=True)
class ProfilePreflight:
    profile: str
    version: int
    tools: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"profile": self.profile, "version": self.version, "tools": list(self.tools)}


def preflight_profile(profile: str) -> ProfilePreflight:
    if profile not in PROFILE_NAMES:
        raise ValueError("unknown tool profile")
    tools = {
        "interactive_unrestricted": ("powershell", "read_artifact", "list_skills", "read_skill", "read_skill_resource"),
        "no_tools": (),
        "nmap_optional": ("nmap_tcp_scan",),
    }[profile]
    if profile == "nmap_optional" and "powershell" in tools:
        raise ValueError("nmap_optional cannot include a host command tool")
    return ProfilePreflight(profile, PROFILE_VERSION, tools)


def build_profile_registry(
    profile: str,
    *,
    context: RunContext | None = None,
    bridge: SandboxBridgeClient | None = None,
    manager: NmapMcpManager | None = None,
    settings: AppSettings | None = None,
) -> ToolRegistry:
    preflight_profile(profile)
    registry = ToolRegistry()
    if profile == "interactive_unrestricted":
        from .powershell import POWERSHELL_SCHEMA, powershell

        registry.register(
            "powershell",
            POWERSHELL_SCHEMA,
            powershell,
            pass_turn=True,
            cancellable=True,
            side_effect=True,
        )
        _register_read_tools(registry)
    elif profile == "nmap_optional":
        if context is None or bridge is None:
            raise ValueError("nmap_optional requires run context and bridge")
        proxy = NmapProxy(manager or NmapMcpManager(context), bridge)
        registry.register(
            "nmap_tcp_scan",
            NMAP_SCHEMA,
            proxy.scan,
            pass_turn=True,
            cancellable=True,
            side_effect=True,
        )
    return registry


def _register_read_tools(registry: ToolRegistry) -> None:
    registry.register("read_artifact", READ_ARTIFACT_SCHEMA, read_artifact, pass_turn=True, read_only=True)
    registry.register("list_skills", LIST_SKILLS_SCHEMA, list_skills, read_only=True)
    registry.register("read_skill", READ_SKILL_SCHEMA, read_skill, read_only=True)
    registry.register("read_skill_resource", READ_SKILL_RESOURCE_SCHEMA, read_skill_resource, read_only=True)
