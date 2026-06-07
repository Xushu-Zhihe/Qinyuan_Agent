# 启动编排层，
# registry.json -> load_registry() -> MCPManager -> start_all() -> MCPToolAdapter
# 创建并启动 MCP client runtime,然后把可用的 manager和 adapter返回给agent主流程
from pathlib import Path #引入 Path,用于构建 registry.json 的路径

from .adapters import MCPToolAdapter
from .config import load_registry
from .manager import MCPManager

#定义异步构建函数
async def build_mcp_runtime(registry_path:str | Path | None=None)->tuple[MCPManager,MCPToolAdapter]:
    # registry 路径
    if registry_path is None:
        registry_path = Path(__file__).resolve().parent / "registry.json"
    # 读取配置文件，得到 MCPRegistryConfig
    registry = load_registry(registry_path)
    # 创建manager ,将registry给予，manager后续会根据registry 里的command/args/...启动MCPServer
    manager = MCPManager(registry)
    # 启动所有的 enabled MCP server
    # stdio_client -> ClientSession -> initialize -> list_tools    
    await manager.start_all()
    # 创建 adapter ，并把 manager 注入，adapter只通过manager 调用工具
    adapter = MCPToolAdapter(manager)
    # manager:用于生命周期控制 adapter: 给agent层调用工具和获取工具说明
    return manager,adapter