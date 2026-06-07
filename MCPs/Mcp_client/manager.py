# 核心层： 把多个 MCP server统一包装成一个可调用的工具池
# 根据配置建立 MCPServer,建立 ClientSession, initialize握手， list_tools获取工具清单，
# call_tools调用工具， stop_all关闭 server/session
from __future__ import annotations

# AsyncExitStack长期持有上下文，最后统一 aclose()
from contextlib import AsyncExitStack

from dataclasses import dataclass,field
from typing import Any
# MCPClient会话， 描述如何启动 server,启动 stdio Mcp server并建立读写流
from mcp import ClientSession,StdioServerParameters,stdio_client
# 从配置层拿已经校验过的配置对象
from .config import MCPRegistryConfig,MCPServerConfig

# 表示一个 MCP工具的元数据，是工具说明
@dataclass(slots=True)
class MCPToolMeta:
    server_name:str # 来自哪个MCP Server 
    tool_name:str # server 原始工作名
    qualified_name:str # 全局工具名，防止同名工具影响
    description:str # agent判断工具用途
    input_schema:dict[str,Any] # agent判断参数如何填写
    tags:list[str] = field(default_factory=list) # 应用侧标签

# 一次工具调用的标准化结果
@dataclass(slots=True)
class MCPToolCallResult:
    server_name:str
    tool_name:str
    qualified_name:str
    is_error:bool # 工具执行是否失败
    text:str | None # MCP 返回的文本内容
    structured: dict[str,Any] | list[Any] | None # MCP返回的结构化数据
    raw_content:list[Any] # 原始 content块
    raw_result:Any # SDK原始结果对象

# 表示一个正在运行或准备运行的 MCPServer状态
@dataclass(slots=True)
class MCPServerRuntime:
    config:MCPServerConfig
    # 负责最终关闭 stadio transport和 session
    exit_stack:AsyncExitStack = field(default_factory=AsyncExitStack)
    session:ClientSession | None = None
    tools:dict[str,MCPToolMeta] = field(default_factory=dict)
    status:str = "stopped"
    last_error:str | None=None

# 管理所有 MCP server和工具索引
class MCPManager:
    # manager初始化接受 registry
    def __init__(self,registry:MCPRegistryConfig) -> None:
        self.registry = registry
        self.runtimes: dict[str,MCPServerRuntime] = {} # 按 server名保存运行
        self.tool_index: dict[str,MCPToolMeta] = {} # 按 qualified_name保存所有工具
    
    #启动所有 enabled MCP server
    async def start_all(self) ->None:
        for config in self.registry.servers.values():
            if not config.enabled: # 跳过 disabled server
                continue
            await self._start_server(config) # 启动单个 server

    #单个 server 的启动逻辑
    async def _start_server(self,config:MCPServerConfig) -> None:
        if config.name in self.runtimes: # 如果已经启动过，就直接返回，避免重复启动
            return 

        runtime = MCPServerRuntime(config=config)
        runtime.status = "starting" # 创建运行时对象，并标记状态为启动中

        try:
            # 把配置转成 SDK需要的启动参数
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env,
            )
            # 启动 stdio server，拿到读写流
            read_stream,write_stream = await runtime.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            # 用读写流创建 MCP client session
            session = await runtime.exit_stack.enter_async_context( # manager要长期持有连接
                ClientSession(read_stream,write_stream)
            )
            # 执行 MCP初始化握手
            await session.initialize()
            runtime.session = session # 把 session保存到 runtime
            runtime.status = "ready"
            self.runtimes[config.name] = runtime # 把 runtime加入 manager

            await self.refresh_tools(config.name) # 启动后立即拉去该 server的工具列表
        # 如果启动失败，清理已打开的资源，并把异常抛出去
        except Exception as exc:
            runtime.status = "failed"
            runtime.last_error = str(exc)
            await runtime.exit_stack.aclose()
            raise
    
    # 关闭所有 MCP server/session 
    async def stop_all(self) ->None: 
        for runtime in self.runtimes.values():
            # 关闭 ClientSession和 stdio_client上下文
            await runtime.exit_stack.aclose()
            runtime.status="stopped"
            # 清空运行时和工具索引
        self.runtimes.clear()
        self.tool_index.clear()
    
    # 刷新工具列表
    async def refresh_tools(self,server_name:str | None = None)->None:
        # 刷新 server_name指向的 server,或者全部
        if server_name is not None and server_name not in self.runtimes:
            raise KeyError(f"unknown server: {server_name}")

        targets = (
            [self.runtimes[server_name]]
            if server_name
            else list(self.runtimes.values())
        ) # 这个变量表示要刷新的 server runtime 列表
        if not server_name: # 若刷新全部，则清空全局工具索引
            self.tool_index.clear()

        for runtime in targets:
            if runtime.session is None:
                continue
        
            # 从 MCP server拉取工具列表
            result = await runtime.session.list_tools()
            runtime.tools.clear() # 清空当前 server的旧工具列表

            for tool in result.tools: # 遍历 MCP server返回的工具
                qualified_name = f"{runtime.config.name}.{tool.name}"
                # 把 SDK的工具对象转化成自己的工具元数据对象
                meta = MCPToolMeta( 
                    server_name=runtime.config.name,
                    tool_name=tool.name,
                    qualified_name=qualified_name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                    tags=list(runtime.config.tags),
                )
                # 同时保存到 server自己的工具表和 manager全局工具索引
                runtime.tools[qualified_name]=meta
                self.tool_index[qualified_name]=meta

    # 返回所有已经注册的工具
    def list_registered_tools(self) -> list[MCPToolMeta]:
        # 按 qualified_name排序，使debug输出稳定
        return sorted(self.tool_index.values(),key=lambda item:item.qualified_name)
    # 按 agent名过滤工具
    def get_tools_for_agent(self,agent_name:str)->list[MCPToolMeta]:
        # 避免 manager.py和 register.py顶层互相导入导致循环依赖
        from .register import filter_tools_for_agent
        return filter_tools_for_agent(self.list_registered_tools(),agent_name)

    async def call_tool(self,qualified_name:str,arguments:dict[str,Any]) -> MCPToolCallResult:
        # 通过全局工作名找到工具元信息
        meta = self.tool_index.get(qualified_name)
        # 若工具不存在，直接报错
        if meta is None:
            raise KeyError(f"unknown tool:{qualified_name}")
        # 对应的 server runtime
        runtime=self.runtimes.get(meta.server_name)
        # 若 server没启动，就不能调用工具
        if runtime is None or runtime.session is None:
            raise RuntimeError(f"server {meta.server_name!r} is not running")
        # 真正调用 MCP工具
        result = await runtime.session.call_tool(meta.tool_name,arguments=arguments)
        # 准备收集文本结果
        text_chunks:list[str]=[]
        # 遍历 MCP返回的 content block
        for block in getattr(result,"content",[]) or []:
            # 只提取 text类型内容
            if getattr(block,"type",None)=="text" and hasattr(block,"text"):
                text_chunks.append(block.text)
        # 把 SDK原始结果转化成统一结果对象
        return MCPToolCallResult(
            server_name=meta.server_name,
            tool_name=meta.tool_name,
            qualified_name=qualified_name,
            is_error=bool(getattr(result,"isError",False)),
            text="\n".join(text_chunks) if text_chunks else None,
            structured=getattr(result,"structuredContent",None),
            raw_content=list(getattr(result,"content",[]) or []),
            raw_result=result,
        )
        
    