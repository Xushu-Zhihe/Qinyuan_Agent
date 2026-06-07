# 验证 MCP client 链路：
# 读取 register.py->启动 MCP server->列出可用工具->调用一个工具->打印结果->关闭 MCP server
# 引入 asyncio,因为 MCP client的启动、调用、关闭都是异步函数，使用 asyncio.run(main())启动
import asyncio
from pathlib import Path

from .config import load_registry
from .manager import MCPManager
from .adapters import MCPToolAdapter
# 复用 bootstrap.py
#from .bootstrap import build_mcp_runtime
#定义异步入口
async def main() -> None:
#    定义当前目录下的 registry.json
    registry_path = Path(__file__).resolve().parent / "registry.json"
#    读取配置
    registry = load_registry(registry_path)
#    创建 manager并启动所有 enabled MCP server
    manager = MCPManager(registry)
    await manager.start_all()
    #manager,adapter = await build_mcp_runtime()
    #开始保护区
    try:
        #创建适配器，它把 manager的结果转换成 agent更容易消费的 dict
        adapter = MCPToolAdapter(manager)
        #获取当前所有已注册 MCP工具
        tools = manager.list_registered_tools()
        print("registered tools")
        #打印工具名和描述
        for tool in tools:
            print(tool.qualified_name,tool.description)
        #调用工具
        result = await adapter.invoke_mcp_tool(
            "nmap.run_nmap_tcp_scan",
            {"target":"127.0.0.1"}
        )
        print(result)
        #在 finally里关闭MCP server
        #MCP stdio server 是子进程，不关闭会留下 session或进程资源
    finally:
        await manager.stop_all()

if __name__=="__main__":
    asyncio.run(main())