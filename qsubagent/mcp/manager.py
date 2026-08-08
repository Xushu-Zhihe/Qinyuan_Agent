from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from qsubagent.sandbox.contracts import RunContext

from .planner import NmapPlanner, NmapPolicyError


class McpError(RuntimeError):
    pass


class _StdioMcpClient:
    def __init__(self, *, command: tuple[str, ...], cwd: Path, environment: dict[str, str]) -> None:
        self.command = command
        self.cwd = cwd
        self.environment = environment
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def start(self, *, timeout_seconds: float) -> None:
        if self.process is not None:
            return
        try:
            self.process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=self.cwd,
                    env=self.environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            await self.close()
            raise McpError("Nmap MCP failed to start") from exc

    async def call(self, method: str, params: dict[str, object], *, timeout_seconds: float) -> dict[str, object]:
        async with self._lock:
            if self.process is None or self.process.stdin is None or self.process.stdout is None:
                raise McpError("Nmap MCP is not running")
            self._next_id += 1
            request = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
            try:
                self.process.stdin.write((__import__("json").dumps(request, separators=(",", ":")) + "\n").encode())
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout_seconds)
            except (OSError, asyncio.TimeoutError) as exc:
                raise McpError("Nmap MCP request failed") from exc
            if not line:
                raise McpError("Nmap MCP exited before responding")
            try:
                payload = __import__("json").loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise McpError("Nmap MCP returned invalid JSON-RPC") from exc
            if not isinstance(payload, dict) or payload.get("id") != self._next_id:
                raise McpError("Nmap MCP response id mismatch")
            if "error" in payload:
                detail = payload["error"]
                raise McpError(str(detail)[:300])
            result = payload.get("result")
            if not isinstance(result, dict):
                raise McpError("Nmap MCP result must be an object")
            return result

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            with __import__("contextlib").suppress(OSError):
                process.stdin.close()
        with __import__("contextlib").suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(process.wait(), timeout=2.0)
        if process.returncode is None:
            with __import__("contextlib").suppress(ProcessLookupError):
                process.kill()


class NmapMcpManager:
    """Own one private MCP runtime per provider run and start it lazily."""

    def __init__(
        self,
        context: RunContext,
        *,
        startup_timeout_seconds: float = 5.0,
        call_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 2.0,
        command: tuple[str, ...] | None = None,
        cwd: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.context = context
        self.planner = NmapPlanner(context.authorized_targets, max_timeout_seconds=900)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.command = command or (
            sys.executable,
            "-m",
            "MCP.Nmap_Mcp.server",
        )
        self.cwd = cwd or Path(__file__).resolve().parents[2]
        self._client = client
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._closed:
            raise McpError("Nmap MCP manager is closed")
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            if self._client is None:
                environment = {
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                    "PYTHONPATH": str(self.cwd),
                }
                for name in ("SYSTEMROOT", "WINDIR", "PATH"):
                    if name in os.environ:
                        environment[name] = os.environ[name]
                self._client = _StdioMcpClient(
                    command=self.command,
                    cwd=self.cwd,
                    environment=environment,
                )
            await self._client.start(timeout_seconds=self.startup_timeout_seconds)
            if isinstance(self._client, _StdioMcpClient):
                await self._client.call(
                    "initialize",
                    {
                        "protocol_version": 1,
                        "authorized_targets": list(self.context.authorized_targets),
                    },
                    timeout_seconds=self.startup_timeout_seconds,
                )
            elif hasattr(self._client, "initialize"):
                await self._client.initialize(self.context)
            self._started = True

    async def plan(
        self,
        *,
        call_id: str,
        target: str,
        ports: object,
        timeout_seconds: object = 60,
    ) -> dict[str, object]:
        # Validate before lazy startup so malformed or unauthorized input has
        # no MCP process side effect.
        expected = self.planner.plan(
            call_id=call_id,
            target=target,
            ports=ports,
            timeout_seconds=timeout_seconds,
        ).command.to_dict()
        await self.start()
        if isinstance(self._client, _StdioMcpClient):
            result = await self._client.call(
                "nmap_tcp_scan",
                {
                    "call_id": call_id,
                    "target": target,
                    "ports": ports,
                    "timeout_seconds": timeout_seconds,
                },
                timeout_seconds=max(self.call_timeout_seconds, float(timeout_seconds) + 1),
            )
        else:
            result = await self._client.plan(
                call_id=call_id,
                target=target,
                ports=ports,
                timeout_seconds=timeout_seconds,
            )
        if not isinstance(result, dict) or set(result) != {
            "protocol_version", "call_id", "tool", "arguments", "timeout_seconds"
        }:
            raise McpError("Nmap MCP returned an invalid sandbox command")
        if result != expected:
            raise McpError("Nmap MCP command differs from the validated plan")
        return result

    async def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        self._started = False
        if client is not None:
            await client.close()


def build_fake_mcp_client(context: RunContext) -> Any:
    """Small deterministic client useful for contract tests without a child process."""

    class FakeClient:
        def __init__(self) -> None:
            self.started = False
            self.closed = False
            self.planner = NmapPlanner(context.authorized_targets)

        async def start(self, *, timeout_seconds: float) -> None:
            self.started = True

        async def plan(self, **kwargs: object) -> dict[str, object]:
            return self.planner.plan(**kwargs).command.to_dict()

        async def close(self) -> None:
            self.closed = True

    return FakeClient()
