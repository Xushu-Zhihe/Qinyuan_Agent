from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from langchain_core.tools import StructuredTool

from MCPs.Mcp_client import AgentMCPClient, MCPManager


NMAP_QUALIFIED_NAME = "nmap.run_nmap_tcp_scan"

# 保留已经使用过的 Nmap LangChain Tool 名称，避免破坏现有 Worker Prompt。
_PUBLIC_NAME_OVERRIDES = {
    NMAP_QUALIFIED_NAME: "nmap_tcp_scan",
}


def _tool_failure(exc: Exception) -> dict[str, Any]:
    """将 MCP/Sandbox 错误返回给 Agent，绝不回退到宿主机命令执行。"""
    return {
        "ok": False,
        "status": "failed",
        "error": type(exc).__name__,
        "message": str(exc),
    }


def _public_tool_name(qualified_name: str) -> str:
    """把 server.tool 转换成 LangChain 兼容且不冲突的工具名。"""
    override = _PUBLIC_NAME_OVERRIDES.get(qualified_name)
    if override is not None:
        return override
    return re.sub(r"[^A-Za-z0-9_-]", "_", qualified_name)


def create_mcp_structured_tool(
    client: AgentMCPClient,
    *,
    qualified_name: str,
    description: str,
    input_schema: dict[str, Any],
) -> StructuredTool:
    """
    把 MCP Tool 动态包装成绑定 Agent 身份的 LangChain StructuredTool。

    agent_id 不属于 args_schema，因此 LLM 无法伪造或切换沙盒。
    """

    def call_mcp_tool(**arguments: Any) -> dict[str, Any]:
        try:
            result = client.call_tool_sync(
                qualified_name,
                arguments,
            )
            return result.to_tool_payload()
        except Exception as exc:
            return _tool_failure(exc)

    async def acall_mcp_tool(**arguments: Any) -> dict[str, Any]:
        try:
            result = await client.call_tool(
                qualified_name,
                arguments,
            )
            return result.to_tool_payload()
        except Exception as exc:
            return _tool_failure(exc)

    return StructuredTool(
        name=_public_tool_name(qualified_name),
        description=(
            description.strip()
            or f"Execute {qualified_name} in this Agent's dedicated sandbox."
        ),
        args_schema=input_schema,
        func=call_mcp_tool,
        coroutine=acall_mcp_tool,
    )


def create_nmap_mcp_tool(
    client: AgentMCPClient,
) -> StructuredTool:
    """兼容旧调用：从 MCP 元数据动态创建 Nmap Tool。"""
    spec = next(
        (
            item
            for item in client.list_tool_specs()
            if item["qualified_name"] == NMAP_QUALIFIED_NAME
        ),
        None,
    )
    if spec is None:
        raise KeyError(f"MCP tool {NMAP_QUALIFIED_NAME!r} is not registered")
    return create_mcp_structured_tool(
        client,
        qualified_name=spec["qualified_name"],
        description=spec["description"],
        input_schema=spec["input_schema"],
    )


def create_agent_tools(
    agent_id: str,
    mcp_manager: MCPManager,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> list[StructuredTool]:
    """
    为一个 Agent 创建专属工具集合。

    allowed_tools=None 表示使用当前已启动 MCP Server 暴露的全部工具。
    Orchestrator 可以根据 Intent 传入更小的 qualified-name 白名单。
    """
    registered = {
        meta.qualified_name
        for meta in mcp_manager.list_registered_tools()
    }
    requested = registered if allowed_tools is None else frozenset(allowed_tools)
    unknown = set(requested).difference(registered)
    if unknown:
        raise KeyError(f"Unknown MCP tools: {sorted(unknown)}")

    client = mcp_manager.bind_agent(
        agent_id,
        allowed_tools=requested,
    )

    return [
        create_mcp_structured_tool(
            client,
            qualified_name=spec["qualified_name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
        )
        for spec in client.list_tool_specs()
    ]


def create_agent_tool_registry(
    agent_id: str,
    mcp_manager: MCPManager,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> dict[str, StructuredTool]:
    """生成 SkillAgentFactory 使用的 public-name -> Tool 映射。"""
    tools = create_agent_tools(
        agent_id,
        mcp_manager,
        allowed_tools=allowed_tools,
    )
    return {tool.name: tool for tool in tools}
