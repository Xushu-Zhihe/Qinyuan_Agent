from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from qsubagent.mcp.planner import NmapPlanner, NmapPolicyError


class NmapMcpServer:
    """Pure validation server: it plans argv and never executes a command."""

    def __init__(self) -> None:
        self._planner: NmapPlanner | None = None

    def dispatch(self, method: str, params: object) -> dict[str, object]:
        if not isinstance(params, dict):
            raise NmapPolicyError("params must be an object")
        if method == "initialize":
            if set(params) != {"protocol_version", "authorized_targets"}:
                raise NmapPolicyError("invalid initialize parameters")
            if params["protocol_version"] != 1:
                raise NmapPolicyError("unsupported protocol version")
            targets = params["authorized_targets"]
            if not isinstance(targets, list) or any(
                not isinstance(item, str) for item in targets
            ):
                raise NmapPolicyError("authorized_targets must be a string list")
            self._planner = NmapPlanner(targets)
            return {
                "protocol_version": 1,
                "server": "qinyuan-nmap-mcp",
                "tools": ["nmap_tcp_scan"],
            }
        if method == "nmap_tcp_scan":
            if self._planner is None:
                raise NmapPolicyError("server is not initialized")
            expected = {"call_id", "target", "ports", "timeout_seconds"}
            if set(params) != expected:
                raise NmapPolicyError("invalid nmap_tcp_scan parameters")
            return self._planner.plan(
                call_id=params["call_id"],
                target=params["target"],
                ports=params["ports"],
                timeout_seconds=params["timeout_seconds"],
            ).command.to_dict()
        if method == "shutdown":
            if params:
                raise NmapPolicyError("shutdown does not accept parameters")
            return {"shutdown": True}
        raise NmapPolicyError("unknown method")


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    server = NmapMcpServer()
    for line in stdin:
        request_id: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {
                "jsonrpc", "id", "method", "params"
            }:
                raise NmapPolicyError("invalid JSON-RPC request")
            if request["jsonrpc"] != "2.0" or not isinstance(request["method"], str):
                raise NmapPolicyError("invalid JSON-RPC envelope")
            request_id = request["id"]
            result = server.dispatch(request["method"], request["params"])
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": type(exc).__name__,
                    "detail": str(exc)[:300],
                },
            }
        stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


if __name__ == "__main__":
    serve()
