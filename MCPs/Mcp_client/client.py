from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass,field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

@dataclass(slots=True)
class MCPServerConfig:
    name:str
    command:str
    args:list[str]=field(default_factory=list)
    env:dict[str,str]=field(default_factory=dict)
    tags:list[str]=field(default_factory=list)
    enabled:bool=True
    transport: str = "stdio"
    url: str | None = None

@dataclass(slots=True)
class MCPRegistryConfig:
    servers:dict[str,MCPServerConfig]

@dataclass(slots=True)
class MCPToolMeta:
    server_name:str
    tool_name:str
    qualified_name:str
    description:str
    input_schema:dict[str,Any]
    tags:list[str]=field(default_factory=list)

@dataclass(slots=True)
class MCPResourceMeta:
    server_name: str
    uri: str
    name: str
    description: str
    mime_type: str | None
    tags: list[str] = field(default_factory=list)

@dataclass(slots=True)
class MCPPromptMeta:
    server_name: str
    prompt_name: str
    qualified_name: str
    description: str
    arguments: list[Any]
    tags: list[str] = field(default_factory=list)

@dataclass(slots=True)
class MCPToolCallResult:
    server_name:str
    tool_name:str
    qualified_name:str
    is_error:bool
    text:str|None
    structured:dict[str,Any] | list[Any] | None
    raw_content:list[Any]
    raw_result:Any

@dataclass(slots=True)
class MCPServerRuntime:
    config:MCPServerConfig
    exit_stack:AsyncExitStack=field(default_factory=AsyncExitStack)
    session:ClientSession | None=None
    tools:dict[str,MCPToolMeta]=field(default_factory=dict)
    status:str="stopped"
    last_error:str | None=None
    resources: dict[str, MCPResourceMeta] = field(default_factory=dict)
    prompts: dict[str, MCPPromptMeta] = field(default_factory=dict)
    server_capabilities: Any | None = None
    server_info: Any | None = None

DEFAULT_AGENT_TAG_POLICY:dict[str,set[str]]={
    "recon":{"recon"},
    "analyzer": {"recon", "fingerprint", "analyzer"},
    "exploit": {"exploit"},
    "writeup": set(),
}

def load_registry(path:str|Path) -> MCPRegistryConfig:
    raw=json.loads(Path(path).read_text(encoding="utf-8"))
    raw_servers = raw.get("mcpServers")

    if not isinstance(raw_servers,dict):
        raise ValueError("registry must contain an object field named 'mcpServers'")
    
    servers:dict[str,MCPServerConfig]={}

    for name,item in raw_servers.items():
        if not isinstance(item,dict):
            raise ValueError(f"server config for {name!r} must be an object")

        command=item.get("command")
        args=item.get("args",[])
        env=item.get("env",{})
        tags=item.get("tags",[])
        enabled=item.get("enabled",True)
        transport = item.get("transport", "stdio")
        url = item.get("url")

        if not isinstance(command,str) or not command:
            raise ValueError(f"{name!r}.command must be a non-empty string")
        
        if not isinstance(args,list) or not all(isinstance(x,str) for x in args):
            raise ValueError(f"{name!r}.args must be a list[str]")
        
        if not isinstance(env,dict) or not all(
            isinstance(k,str) and isinstance(v,str)
            for k,v in env.items()
        ):
            raise ValueError(f"{name!r}.env must be a dict[str,str]")
        
        if not isinstance(tags,list) or not all(isinstance(x,str) for x in tags):
            raise ValueError(f"{name!r}.tags must be a list[str]") 
    
        if not isinstance(enabled,bool):
            raise ValueError(f"{name!r}.enabled must be a bool")
        
        servers[name]=MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            tags=tags,
            enabled=enabled,
            transport=transport,
            url=url,
        )

    return MCPRegistryConfig(servers=servers)
            

class MCPManager:
    def __init__(
        self,
        registry:MCPRegistryConfig,
        agent_tag_policy:dict[str,set[str]] | None=None,
    )->None:
        self.registry=registry
        self.agent_tag_policy=agent_tag_policy or DEFAULT_AGENT_TAG_POLICY
        self.runtimes:dict[str,MCPServerRuntime] = {}
        self.tool_index:dict[str,MCPToolMeta]={}
        self.resource_index:dict[str,MCPResourceMeta]={}
        self.prompt_index:dict[str,MCPPromptMeta]={}

    async def start_all(self)->None:
        for config in self.registry.servers.values():
            if config.enabled:
                await self._start_server(config)

    async def _start_server(self,config:MCPServerConfig) -> None:
        if config.name in self.runtimes:
            return
        
        runtime = MCPServerRuntime(config)
        runtime.status = "starting"

        try:
            if config.transport == "stdio":
                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=config.env,
                )

                read_stream, write_stream = await runtime.exit_stack.enter_async_context(
                    stdio_client(server_params)
                )

            elif config.transport == "streamable_http":
                if not config.url:
                    raise ValueError(f"{config.name!r}.url is required for streamable_http")

                read_stream, write_stream, _ = await runtime.exit_stack.enter_async_context(
                    streamablehttp_client(config.url)
                )

            else:
                raise ValueError(f"unsupported transport: {config.transport}")

            session = await runtime.exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            init_result = await session.initialize()

            runtime.session = session
            runtime.server_capabilities = getattr(init_result, "capabilities", None)
            runtime.server_info = getattr(init_result, "serverInfo", None)
            runtime.status = "ready"
            self.runtimes[config.name] = runtime

            await self.refresh_tools(config.name)
            await self.refresh_resources(config.name)
            await self.refresh_prompts(config.name)
    

        except Exception as exc:
            runtime.status = "failed"
            runtime.last_error = str(exc)
            await runtime.exit_stack.aclose()
            raise
    
    async def stop_all(self) -> None:
        for runtime in reversed(list(self.runtimes.values())):
            await runtime.exit_stack.aclose()
            runtime.status = "stopped"
        
        self.runtimes.clear()
        self.tool_index.clear()

    async def refresh_tools(self,server_name:str | None = None) -> None:
        if server_name is not None and server_name not in self.runtimes:
            raise KeyError(f"unknown server: {server_name}")
        
        targets = (
            [self.runtimes[server_name]]
            if server_name
            else list(self.runtimes.values())
        )

        if server_name is None:
            self.tool_index.clear()

        for runtime in targets:
            if runtime.session is None:
                continue

            result = await runtime.session.list_tools()
            runtime.tools.clear()

            for tool in result.tools:
                qualified_name = f"{runtime.config.name}.{tool.name}"

                meta = MCPToolMeta(
                    server_name=runtime.config.name,
                    tool_name=tool.name,
                    qualified_name=qualified_name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                    tags=list(runtime.config.tags),
                )

                runtime.tools[qualified_name] = meta
                self.tool_index[qualified_name] = meta

    async def refresh_resources(self,server_name: str | None = None) -> None:
        targets =(
            [self.runtimes[server_name]] 
            if server_name
            else list(self.runtimes.values())
        )

        if server_name is None:
            self.resource_index.clear()

        for runtime in targets:
            if runtime.session is None:
                continue

            result = await runtime.session.list_resources()
            runtime.resources.clear()

            for resource in result.resources:
                uri = str(resource.uri)
                key = f"{runtime.config.name}:{uri}"
                meta = MCPResourceMeta(
                    server_name=runtime.config.name,
                    uri=uri,
                    name=getattr(resource,"name",uri),
                    description=getattr(resource,"description","") or "",
                    mime_type=getattr(resource,"mimeType",None),
                    tags=list(runtime.config.tags),
                )
                runtime.resources[key] = meta
                self.resource_index[key] = meta

    async def read_resource(self,qualified_uri:str) -> Any:
        meta = self.resource_index[qualified_uri]
        runtime = self.runtimes.get(meta.server_name)

        if runtime is None or runtime.session is None:
            raise RuntimeError(f"server {meta.server_name!r} is not running")
        
        return await runtime.session.read_resource(meta.uri)

    async def refresh_prompts(self,server_name: str | None = None) -> None:
        targets = [self.runtimes[server_name]] if server_name else list(self.runtimes.values())

        if server_name is None:
            self.prompt_index.clear()

        for runtime in targets:
            if runtime.session is None:
                continue

            result = await runtime.session.list_prompts()
            runtime.prompts.clear()

            for prompt in result.prompts:
                qualified_name = f"{runtime.config.name}.{prompt.name}"
                meta = MCPPromptMeta(
                    server_name=runtime.config.name,
                    prompt_name=prompt.name,
                    qualified_name=qualified_name,
                    description=prompt.description or "",
                    arguments=list(getattr(prompt,"arguments",[]) or []),
                    tags=list(runtime.config.tags),
                )
                runtime.prompts[qualified_name] = meta
                self.prompt_index[qualified_name] = meta
    
    async def get_prompt(self,qualified_name:str,arguments:dict[str,Any] | None = None) -> Any:
        meta: MCPPromptMeta | None = self.prompt_index.get(qualified_name)
        
        if meta is None:
            raise KeyError(f"unknown prompt: {qualified_name}")

        runtime = self.runtimes.get(meta.server_name)

        if runtime is None or runtime.session is None:
            raise RuntimeError(f"server {meta.server_name!r} is not running")   

        return await runtime.session.get_prompt(meta.prompt_name,arguments=arguments or {})    

    def list_registered_tools(self) -> list[MCPToolMeta]:
        return sorted(
            self.tool_index.values(),
            key = lambda item: item.qualified_name,
        )

    def get_tools_for_agent(self,agent_name:str) -> list[MCPToolMeta]:
        allowed_tags = self.agent_tag_policy.get(agent_name,set())

        if not allowed_tags:
            return []
        
        return [
            tool 
            for tool in self.list_registered_tools()
            if allowed_tags.intersection(tool.tags)
        ]
    
    async def call_tool(
        self,
        qualified_name:str,
        arguments:dict[str,Any],
    ) -> MCPToolCallResult:
        meta = self.tool_index.get(qualified_name)

        if meta is None:
            raise KeyError(f"unknown tool: {qualified_name}")
        
        runtime = self.runtimes.get(meta.server_name)

        if runtime is None or runtime.session is None:
            raise RuntimeError(f"server {meta.server_name!r} is not running")


        result = await runtime.session.call_tool(
            meta.tool_name,
            arguments=arguments,
        )

        text_chunks: list[str] = []

        for block in getattr(result,"content",[]) or []:
            if getattr(block,"type",None) == "text" and hasattr(block,"text"):
                text_chunks.append(block.text)

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
    
class MCPToolAdapter:
    def __init__(self,manager:MCPManager) -> None:
        self.manager = manager 

    async def invoke_mcp_tool(
        self,
        qualified_name:str,
        arguments:dict[str,Any],
    ) -> dict[str,Any]:
        result = await self.manager.call_tool(qualified_name,arguments)
        return self.normalize_tool_result(result)
    
    def list_tool_specs_for_agent(self,agent_name:str) -> list[dict[str,Any]]:
        tools = self.manager.get_tools_for_agent(agent_name)
        return [self._tool_meta_to_dict(tool) for tool in tools]
    
    def list_all_tool_specs(self) -> list[dict[str,Any]]:
        tools = self.manager.list_registered_tools()
        return [self._tool_meta_to_dict(tool) for tool in tools]
    
    @staticmethod
    def normalize_tool_result(result:MCPToolCallResult) -> dict[str,Any]:
        payload: dict[str,Any]={
            "server":result.server_name,
            "tool":result.tool_name,
            "qualified_name":result.qualified_name,
            "is_error":result.is_error,
            "text":result.text,
        }

        if result.structured is not None:
            payload["data"] = result.structured

        return payload

    @staticmethod
    def _tool_meta_to_dict(tool:MCPToolMeta) -> dict[str,Any]:
        return{
            "server":tool.server_name,
            "tool":tool.tool_name,
            "qualified_name":tool.qualified_name,
            "description":tool.description,
            "input_schema":tool.input_schema,
            "tags":tool.tags,
        }
    
async def build_mcp_runtime(
        registry_path: str | Path | None = None,
        agent_tag_policy: dict[str,set[str]] | None = None,
) -> tuple[MCPManager,MCPToolAdapter]:
    if registry_path is None:
        registry_path = Path(__file__).resolve().parent / "registry.json"

    registry = load_registry(registry_path)

    manager = MCPManager(
        registry=registry,
        agent_tag_policy=agent_tag_policy,
    )            

    await manager.start_all()

    adapter = MCPToolAdapter(manager)

    return manager,adapter
