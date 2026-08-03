from __future__ import annotations

import json
from typing import TYPE_CHECKING

from qsubagent.config import ArtifactSettings
from qsubagent.runtime import ArtifactStore, ToolResult

from .registry import registry

if TYPE_CHECKING:
    from qsubagent.runtime import ActiveTurn


def read_artifact(
    artifact: str,
    start_byte: int = 0,
    max_bytes: int = 65_536,
    start_line: int | None = None,
    line_count: int = 200,
    turn: "ActiveTurn | None" = None,
) -> ToolResult:
    store = (
        turn.artifact_store
        if turn is not None and turn.artifact_store is not None
        else ArtifactStore(ArtifactSettings())
    )
    try:
        result = store.read(
            artifact,
            start_byte=start_byte,
            max_bytes=max_bytes,
            start_line=start_line,
            line_count=line_count,
        )
    except Exception as exc:
        return ToolResult(
            output=json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            status="failed",
        )
    return ToolResult(output=json.dumps(result, ensure_ascii=False))


READ_ARTIFACT_SCHEMA = {
    "type": "function",
    "name": "read_artifact",
    "description": "Read a bounded byte range or line range from a tool artifact.",
    "parameters": {
        "type": "object",
        "properties": {
            "artifact": {"type": "string"},
            "start_byte": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1},
            "start_line": {"type": ["integer", "null"], "minimum": 1},
            "line_count": {"type": "integer", "minimum": 1},
        },
        "required": ["artifact"],
        "additionalProperties": False,
    },
    "strict": False,
}

registry.register(
    "read_artifact",
    READ_ARTIFACT_SCHEMA,
    read_artifact,
    pass_turn=True,
    cancellable=False,
    side_effect=False,
    read_only=True,
)
