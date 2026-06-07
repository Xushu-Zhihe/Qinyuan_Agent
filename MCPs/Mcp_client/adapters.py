# 将agent调用请求转给 MCPManager.call_tool()
# 把MCPToolCallResult 转成 agent 容易理解消费的 dict
from __future__ import annotations

from typing import Any

from .manager import MCPManager, MCPToolCallResult, MCPToolMeta

class MCPToolAdapter:#定义适配器类
    def __init__(self,manager:MCPManager) -> None:#构造时注入MCPManager
        self.manager = manager

    #访问 self.manager
    async def invoke_mcp_tool(self,qualified_name:str,arguments:dict[str,Any])->dict[str,Any]:
        result = await self.manager.call_tool(qualified_name,arguments)
        return self.normalize_tool_result(result)
   
    # 给 agent获取“可用工具说明”
    def list_tool_specs_for_agent(self,agent_name:str)->list[dict[str,Any]]:
        # 向 manager查询某个agent可见的工具
        tools=self.manager.get_tools_for_agent(agent_name)
        # 把内部的 MCPToolMeta 对象转化为 dict
        return [self._tool_meta_to_dict(tool) for tool in tools]

    @staticmethod
    # 将 MCP调用结果转换成统一的输出格式
    def normalize_tool_result(result:MCPToolCallResult) -> dict[str,Any]:
        # 标准化结果的主体，agent只需要看统一字段，不需要理解 MCP原始返回对象
        payload:dict[str,Any] = {
            "server":result.server_name,
            "tool":result.tool_name,
            "qualified_name":result.qualified_name,
            "is_error":result.is_error,
            "text":result.text,
        }
        # 如果 MCP server 返回了结构化数据，就放到 data
        if result.structured is not None:
            payload["data"]=result.structured

        return payload
    
    @staticmethod
    # 把工具元数据转换成字典，供agent选择工具时使用
    def _tool_meta_to_dict(tool:MCPToolMeta) -> dict[str,Any]:
        return {
            "server":tool.server_name,
            "tool":tool.tool_name,
            "qualified_name":tool.qualified_name,
            "description":tool.description,# agent判断说明时候调用工具
            "input_schema":tool.input_schema,# 给agent判断参数如何填
            "tags":tool.tags, #给recon/analyzer/exploit/writeup做工具过滤
        }
