from __future__ import annotations

import asyncio
import json
import os

from MCPs.Mcp_client import build_mcp_runtime
from Sandbox.docker_runtime import DockerRuntime
from Sandbox.service import SandboxService


async def main() -> None:
    agent_id = "nmap-debug-agent"
    target = os.getenv("QINYUAN_TEST_TARGET", "127.0.0.1")

    sandbox_service: SandboxService | None = None
    manager = None

    try:
        runtime = DockerRuntime()
        sandbox_service = SandboxService(runtime)

        session = sandbox_service.start_for_agent(agent_id)
        print(f"Agent sandbox started: {session.sandbox_id}")

        manager = await build_mcp_runtime(sandbox_service)

        client = manager.bind_agent(
            agent_id,
            allowed_tools={"nmap.run_nmap_tcp_scan"},
        )

        result = await client.call_tool(
            "nmap.run_nmap_tcp_scan",
            {
                "target": target,
                "ports": "80,443",
                "timeout_seconds": 30,
            },
        )

        print(
            json.dumps(
                result.to_tool_payload(),
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        try:
            if manager is not None:
                await manager.stop_all()
        finally:
            if sandbox_service is not None:
                sandbox_service.close()


if __name__ == "__main__":
    asyncio.run(main())