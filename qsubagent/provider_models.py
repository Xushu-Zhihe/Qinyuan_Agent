from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderProcessResult:
    status: str
    exit_code: int
    protocol_line: str | None = None
    error: str | None = None
    tool_summary: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": 1,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "tool_summary": list(self.tool_summary),
        }
