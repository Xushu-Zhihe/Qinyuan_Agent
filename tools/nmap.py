from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qsubagent.mcp.manager import McpError, NmapMcpManager
from qsubagent.mcp.planner import NmapPolicyError
from qsubagent.runtime import ToolResult
from qsubagent.sandbox.client import SandboxBridgeClient
from qsubagent.sandbox.contracts import BridgeProtocolError, CommandRequest
from qsubagent.sandbox.errors import SandboxBridgeError

if TYPE_CHECKING:
    from qsubagent.runtime import ActiveTurn


NMAP_SCHEMA = {
    "type": "function",
    "name": "nmap_tcp_scan",
    "description": (
        "Plan a TCP connect Nmap scan for an authorized target. "
        "Only the managed Nmap sandbox executes the resulting command."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "minLength": 1},
            "ports": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 65535},
                "minItems": 1,
                "maxItems": 128,
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 900,
            },
        },
        "required": ["target", "ports", "timeout_seconds"],
        "additionalProperties": False,
    },
    "strict": True,
}


class NmapProxy:
    def __init__(self, manager: NmapMcpManager, bridge: SandboxBridgeClient) -> None:
        self.manager = manager
        self.bridge = bridge

    async def scan(
        self,
        *,
        target: str,
        ports: object,
        timeout_seconds: float,
        turn: "ActiveTurn | None" = None,
    ) -> ToolResult:
        call_id = turn.running_call_id if turn is not None else None
        if not call_id:
            return _failed("nmap requires a model call id")
        try:
            raw_command = await self.manager.plan(
                call_id=call_id,
                target=target,
                ports=ports,
                timeout_seconds=timeout_seconds,
            )
            command = CommandRequest.from_dict(raw_command)
        except (NmapPolicyError, McpError, BridgeProtocolError) as exc:
            return _failed(type(exc).__name__, str(exc))

        if turn is not None:
            turn.cancel_callback = self._cancel_callback(call_id)
        try:
            result = await self.bridge.execute(command)
        except SandboxBridgeError as exc:
            status = "outcome_unknown" if exc.request_may_have_started else "failed"
            return _failed(
                exc.category,
                str(exc),
                status=status,
                uncertain=exc.request_may_have_started,
            )
        finally:
            if turn is not None:
                turn.cancel_callback = None

        payload = {
            "ok": result.status == "completed" and result.exit_code == 0,
            "status": result.status,
            "exit_code": result.exit_code,
            "stdout": _bounded_text(result.stdout),
            "stderr": _bounded_text(result.stderr),
            "timed_out": result.timed_out,
            "stdout_truncated": result.stdout_truncated or len(result.stdout.encode("utf-8")) > 32 * 1024,
            "stderr_truncated": result.stderr_truncated or len(result.stderr.encode("utf-8")) > 32 * 1024,
            "artifact_refs": list(result.artifact_refs),
        }
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            status=_map_status(result.status),
            exit_code=result.exit_code,
            metadata={
                "call_id": result.call_id,
                "artifact_refs": list(result.artifact_refs),
                "timed_out": result.timed_out,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "side_effect": True,
            },
        )

    def _cancel_callback(self, call_id: str) -> Callable[[], Awaitable[bool]]:
        async def cancel() -> bool:
            try:
                return await self.bridge.cancel(call_id)
            except Exception:
                return False

        return cancel


def _map_status(status: str) -> str:
    if status == "cancelled":
        return "aborted"
    if status not in {"completed", "failed", "timeout", "aborted", "outcome_unknown"}:
        return "failed"
    return status


def _bounded_text(value: str, limit: int = 32 * 1024) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    head = limit // 2
    tail = limit - head
    return (
        encoded[:head].decode("utf-8", errors="ignore")
        + "\n...[middle omitted]...\n"
        + encoded[-tail:].decode("utf-8", errors="ignore")
    )


def _failed(
    error: str,
    message: str | None = None,
    *,
    status: str = "failed",
    uncertain: bool = False,
) -> ToolResult:
    return ToolResult(
        output=json.dumps(
            {"ok": False, "error": error, "message": message or error},
            ensure_ascii=False,
        ),
        status=status,
        metadata={"side_effect": True, "outcome_unknown": uncertain},
    )


def build_nmap_proxy_schema() -> dict[str, Any]:
    return json.loads(json.dumps(NMAP_SCHEMA))
