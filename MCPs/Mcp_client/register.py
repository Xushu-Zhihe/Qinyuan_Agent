# agent工具权限策略层
# 根据 agent角色，过滤其能看到哪些 MCP工具
# 所有 MCP工具 -> register.py按 tag过滤 -> 根据 agent自己的tag,agent只能看到允许的工具
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
        from .manager import MCPToolMeta

AGENT_TAG_POLICY: dict[str, set[str]] = { 
    # 定义一个全局策略表， key是 agent名称， value是这个 agent允许使用的工具标签集合
    "recon":{"recon"},# recon agent 只能看到带有 recon 标签的 MCP 工具
    "analyzer":{"recon","fingerprint"},
    "exploit":{"exploit"},
    "writeup":set(),
}

# 定义过滤函数，输入所有工具 all_tools,以及当前 agent名称 agent_name，
# 返回这个 agent允许看到的工具列表
def filter_tools_for_agent(
    all_tools: list["MCPToolMeta"],
    agent_name:str
    ) -> list["MCPToolMeta"]:
    # 从策略表中取出当前 agent允许的标签，若 agent不存在，就返回空集合
    allowed_tags = AGENT_TAG_POLICY.get(agent_name,set())
    # 如果当前 agent没有允许标签，直接返回空列表
    if not allowed_tags:
        return []
    # 只要工具的 tool.tags和当前的 agent允许的 allowed_tags有交集，就允许此 agent看到它
    return [tool for tool in all_tools if allowed_tags.intersection(tool.tags)]

