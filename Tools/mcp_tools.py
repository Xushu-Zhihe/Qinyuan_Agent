from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from MCPs.Mcp_client import (
    AgentMCPClient,
    MCPManager,
)

NMAP_QUALIFIED_NAME = "nmap.run_nmap_tcp_scan"

class NmapTCPScanInput(BaseModel):
    """LLM 可以提供的 Nmap 参数；不包含 agent_id。"""

    target: str = Field(
        min_length=1,
        max_length=253,
        description=(
            "Authorized target hostname or IP address, such as target-web."
        ),
    )
    ports: str = Field(
        default="80,443,8080",
        min_length=1,
        max_length=4096,
        description=(
            "Comma-separated TCP ports or ranges, such as "
            "'22,80,443,8000-8100'."
        ),
    )
    timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=300,
        description="Maximum scan duration in seconds.",
    )

def _tool_failure(exc: Exception) -> dict[str, Any]:
    """
    将可预期的 MCP/Sandbox 错误返回给 Agent。

    这里不执行命令，也不会自动改用宿主机 subprocess。
    """

    return {
        "ok": False,
        "status": "failed",
        "error": type(exc).__name__,
        "message": str(exc),
    }

def create_nmap_mcp_tool(
    client: AgentMCPClient,
) -> StructuredTool:
    """创建绑定到一个 Agent 专属沙盒的 Nmap Tool。"""

    def run_nmap_tcp_scan(
        target: str,
        ports: str = "80,443,8080",
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        try:
            result = client.call_tool_sync(
                NMAP_QUALIFIED_NAME,
                {
                    "target": target,
                    "ports": ports,
                    "timeout_seconds": timeout_seconds,
                },
            )
            return result.to_tool_payload()
        except Exception as exc:
            return _tool_failure(exc)

    async def arun_nmap_tcp_scan(
        target: str,
        ports: str = "80,443,8080",
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        try:
            result = await client.call_tool(
                NMAP_QUALIFIED_NAME,
                {
                    "target": target,
                    "ports": ports,
                    "timeout_seconds": timeout_seconds,
                },
            )
            return result.to_tool_payload()
        except Exception as exc:
            return _tool_failure(exc)

    return StructuredTool.from_function(
        func=run_nmap_tcp_scan,
        coroutine=arun_nmap_tcp_scan,
        name="nmap_tcp_scan",
        description=(
            "Run an authorized Nmap TCP connect scan inside this "
            "Agent's dedicated Docker sandbox."
        ),
        args_schema=NmapTCPScanInput,
    )

def create_agent_tools(
    agent_id: str,
    mcp_manager: MCPManager,
) -> list[StructuredTool]:
    """
    为一个动态 Agent 创建专属工具。

    每次创建 Agent 都必须调用一次，不能使用全局共享 TOOLS。
    """

    client = mcp_manager.bind_agent(
        agent_id,
        allowed_tools={NMAP_QUALIFIED_NAME},
    )

    return [
        create_nmap_mcp_tool(client),
    ]

def create_agent_tool_registry(
    agent_id: str,
    mcp_manager: MCPManager,
) -> dict[str, StructuredTool]:
    """
    生成 SkillAgentFactory 当前所需的 name -> Tool 映射。
    """

    tools = create_agent_tools(agent_id, mcp_manager)
    return {tool.name: tool for tool in tools}

