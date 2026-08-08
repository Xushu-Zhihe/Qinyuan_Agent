from __future__ import annotations

from dataclasses import dataclass

from qsubagent.sandbox.contracts import PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    protocol_version: int
    call_id: str
    tool: str
    arguments: tuple[str, ...]
    timeout_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "call_id": self.call_id,
            "tool": self.tool,
            "arguments": list(self.arguments),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class NmapScanPlan:
    command: SandboxCommand
    target: str
    ports: tuple[int, ...]


def is_v1_command(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("protocol_version") == PROTOCOL_VERSION
        and set(value)
        == {"protocol_version", "call_id", "tool", "arguments", "timeout_seconds"}
    )
