from __future__ import annotations

import sys
import asyncio
import json
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from Sandbox.models import CommandRequest
from Sandbox.service import SandboxService

class MCPClientError(RuntimeError):
    """MCP客户端、协议或命令计划错误"""

class MCPServerNotReadyError(MCPClientError):
    """MCP Server尚未启动或已经停止"""

class MCPCommandPlanError(MCPClientError):
    """MCP Server返回了非法的沙盒命令计划"""

@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """单个 MCP Server的启动和权限配置"""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str,...] = ()
    cwd: Path | None = None
    env: dict[str,str] = field(default=dict)
    url: str | None = None
    tags: frozenset[set] = frozenset()
    sandbox_tools: frozenset[set] = frozenset()
    enabled: bool = True

@dataclass(frozen=True, slots=True)
class MCPRegistryConfig:
    servers: dict[str, MCPServerConfig]

@dataclass(frozen=True, slots=True)
class MCPToolMeta:
    server_name: str
    tool_name: str
    qualified_name: str
    description: str
    input_schema: dict[str,Any]
    tags: frozenset[str]

@dataclass(slots=True)
class MCPServerRuntime:
    config: MCPServerConfig
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    session: ClientSession | None = None
    tools: dict[str, MCPToolMeta] = field(default_factory=dict)
    status: str = "stopped"
    last_error: str | None = None

@dataclass(frozen=True,slots=True)
class MCPExecutionResult:
    """
    MCP命令计划在 Agent专属沙盒中的实际执行结果

    agent_id和 sandbox_id保留在控制平面中用于审计
    to_tool_payload() 不会把它们暴露给 LLM
    """

    agent_id: str
    sandbox_id: str
    qualified_name: str
    executable: str
    arguments: tuple[str,...]
    status: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_tool_payload(self) -> dict[str,Any]:
        """生成可以安全返回给 Agent的结果"""

        return{
            "ok": self.status == "completed",
            "status": self.status,
            "tool": self.qualified_name,
            "command":[self.executable,*self.arguments],
            "exit_code":self.exit_code,
            "stdout":self.stdout,
            "stderr":self.stderr,
            "stdout_truncated":self.stdout_truncated,
            "stderr_   ":self.stderr_truncated,
        }

def load_registry(path: str | Path) -> MCPRegistryConfig:
    """
    读取 MCP Server注册表

    cwd相对于 registry.json所在目录解析，因此项目移动后不需要修改绝对路径
    """
    
    registry_path = Path(path).resolve()
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw_servers = raw.get("mcpServers")

    if not isinstance(raw_servers,dict):
        raise ValueError("registry must contain an object named 'mcpServers'")

    servers: dict[str, MCPServerConfig] = {}

    for name, item in raw_servers.items():
        if not isinstance(name,str) or not name.strip():
            raise ValueError("MCP server name must be a non-empty string")

        if not isinstance(item,dict):
            raise ValueError(f"server config for {name!r} must be an object")

        transport = item.get("transport","stdio")
        command = item.get("command")
        url = item.get("url")
        enabled = item.get("enabled",True)

        args = _string_list(item,"args")
        tags = frozenset(_string_list(item,"tags"))
        sandbox_tools = frozenset(_string_list(item,"sandboxTools"))

        env = item.get("env",{})
        if not isinstance(env,dict) or not all(
            isinstance(key,str) and isinstance(value,str)
            for key, value in env.items()
        ):
            raise ValueError(f"{name!r}.env must be an object of strings")

        cwd: Path | None = None
        cwd_value = item.get("cwd")
        if cwd_value is not None:
            if not isinstance(cwd_value, str) or not cwd_value.strip():
                raise ValueError(f"{name!r}.cwd must be a non-empty string")

            cwd = Path(cwd_value)
            if not cwd.is_absolute():
                cwd = registry_path.parent / cwd
            cwd = cwd.resolve()

        if transport == "stdio":
            if not isinstance(command,str) or not command.strip():
                raise ValueError(
                    f"{name!r}.command is required for stdio transport"
                )
        elif transport == "streamable_http":
            if not isinstance(url,str) or not url.strip():
                raise ValueError(
                    f"{name!r}.url is required for streamable_http transport"
                )            
        else:
            raise ValueError(
                f"{name!r}.transport must be 'stdio' or 'streamable_http'"
            )

        servers[name] = MCPServerConfig(
            name = name,
            transport = transport,
            command = command,
            args = tuple(args),
            cwd = cwd,
            env = dict(env),
            url=url,
            tags=tags,
            sandbox_tools=sandbox_tools,
            enabled=enabled,
        )

    return MCPRegistryConfig(servers=servers)

def _string_list(item:dict[str,Any], field_name:str) -> list[str]:
    value = item.get(field_name, [])

    if not isinstance(value, list) or not all(
        isinstance(element, str) and element.strip()
        for element in value
    ):
        raise ValueError(f"{field_name} must be a list of non-empty strings")

    return value

class MCPManager:
    """
    MCP 总管：
    1.启动和关闭 MCP Server     
    2.发现 MCP Tools
    3.读取并校验 sandbox_command       
    4.将命令绑定到内部 agent_id
    5.通过 SandboxService在 Agent专属容器中执行
    """

    def __init__(
        self,
        registry: MCPRegistryConfig,
        sandbox_service: SandboxService,
        *,
        mcp_call_timeout_seconds: float = 10.0,
        output_character_limit: int = 20_000,
    ) -> None:
        if mcp_call_timeout_seconds <= 0:
            raise ValueError("mcp_call_timeout_seconds must be positive")

        if output_character_limit < 1_000:
            raise ValueError("output_character_limit must be at least 1000")

        self.registry = registry
        self.sandbox_service = sandbox_service
        self.mcp_call_timeout_seconds = mcp_call_timeout_seconds
        self.output_character_limit = output_character_limit

        self.runtimes: dict[str, MCPServerRuntime] = {}
        self.tool_index: dict[str, MCPToolMeta] = {}

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._event_loop_thread_id: int | None = None

    async def start_all(self) -> None:
        """启动注册表中所有 enabled = true的 MCP Server"""

        if self.runtimes:
            return 

        self._event_loop = asyncio.get_running_loop()
        self._event_loop_thread_id = threading.get_ident()

        try:
            for config in self.registry.servers.values():
                if config.enabled:
                    await self._start_server(config)
        except Exception:
            try:
                await self.stop_all()
            except Exception:
                pass
            raise

    async def _start_server(self,config: MCPServerConfig) -> None:
        if config.name in self.runtimes:
            return 

        runtime = MCPServerRuntime(config=config)
        runtime.status = "starting"

        try:
            if config.transport == "stdio":
                if config.cwd is not None and not config.cwd.is_dir():
                    raise MCPClientError(
                        f"MCP server cwd does not exist: {config.cwd}"
                    )

                command = config.command or ""

                # 使用当前正在运行 MCPManager 的 Python 解释器。
                # 避免写死 .venv/Scripts/python.exe，也避免创建 MCP 独立 uv 环境。
                if command == "{python}":
                    command = sys.executable

                parameters = StdioServerParameters(
                    command=command,
                    args=list(config.args),
                    env=config.env,
                    cwd=config.cwd,
                )

                read_stream,write_stream =(
                    await runtime.exit_stack.enter_async_context(
                        stdio_client(parameters)
                    )
                )
            else:
                read_stream,write_stream,_ = (
                    await runtime.exit_stack.enter_async_context(
                        streamable_http_client(config.url or "")
                    )
                )

            session = await runtime.exit_stack.enter_async_context(
                ClientSession(read_stream,write_stream)
            )

            await session.initialize()

            runtime.session = session
            runtime.status = "ready"
            self.runtimes[config.name] = runtime

            await self.refresh_tools(config.name)

        except Exception as exc:
            runtime.status = "failed"
            runtime.last_error = str(exc)
            self.runtimes.pop(config.name,None) 
            await runtime.exit_stack.aclose()
            raise MCPClientError(
                f"failed to start MCP server {config.name!r}: {exc}"
            ) from exc

    async def stop_all(self) -> None:
        """尽最大可能关闭所有的 MCP Server"""

        errors:list[str] = []

        for runtime in reversed(list(self.runtimes.values())):
            try:
                await runtime.exit_stack.aclose()
            except Exception as exc:
                errors.append(f"{runtime.config.name}: {exc}")
            finally:
                runtime.status = "stopped"
                runtime.session = None

        self.runtimes.clear()
        self.tool_index.clear()
        self._event_loop = None
        self._event_loop_thread_id = None

        if errors:
            raise MCPClientError(
                "one or more MCP servers failed to close: "
                + "; ".join(errors)
            )
        
    async def refresh_tools(self,server_name: str) -> None:
        """重新读取指定 MCP Server的工具清单"""

        runtime = self.runtimes.get(server_name)
        if runtime is None or runtime.session is None:
            raise MCPServerNotReadyError(
                f"MCP server {server_name!r} is not ready"
            )

        result = await runtime.session.list_tools()

        for old_name in runtime.tools:
            self.tool_index.pop(old_name,None)

        runtime.tools.clear()

        for tool in result.tools:
            qualified_name = f"{server_name}.{tool.name}"

            meta = MCPToolMeta(
                server_name=server_name,
                tool_name=tool.name,
                qualified_name=qualified_name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {},
                tags=runtime.config.tags,
            )

            runtime.tools[qualified_name] = meta
            self.tool_index[qualified_name] = meta

    def list_registered_tools(self) -> list[MCPToolMeta]:
        return sorted(
            self.tool_index.values(),
            key=lambda item: item.qualified_name,
        )

    def bind_agent(
        self,
        agent_id: str,
        *,
        allowed_tools: Iterable[str],
    ) -> AgentMCPClient:
        """
        创建永久绑定 agent_id的客户端

        agent_id不会出现在 LLM的工具参数中
        """

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id cannot be empty")

        if self.sandbox_service.get_session(normalized_agent_id) is None:
            raise MCPClientError(
                f"Agent {normalized_agent_id!r} does not have a sandbox"
            )

        allowed = frozenset(allowed_tools)
        unknown = allowed.difference(self.tool_index)

        if unknown:
            raise MCPClientError(
                f"unknown MCP tools: {sorted(unknown)}"
            )

        return AgentMCPClient(
            manager=self,
            agent_id=normalized_agent_id,
            allowed_tools=allowed,
        )

    async def _execute_bound_tool(
        self,
        *,
        agent_id: str,
        qualified_name: str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        meta = self.tool_index.get(qualified_name)
        if meta is None:
            raise MCPClientError(f"unknown MCP tool:{qualified_name}")

        runtime = self.runtimes.get(meta.server_name)
        if runtime is None or runtime.session is None:
            raise MCPServerNotReadyError(
                f"MCP server {meta.server_name!r} is not ready"
            )

        if not isinstance(arguments,dict) or not all(
            isinstance(key,str) for key in arguments
        ):
            raise MCPClientError("MCP tool arguments must be an object")

        try:
            mcp_result = await asyncio.wait_for(
                runtime.session.call_tool(
                    meta.tool_name,
                    arguments=arguments,
                ),
                timeout=self.mcp_call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPClientError(
                f"MCP server {meta.server_name!r} did not produce "
                "a command plan in time"
            ) from exc

        request = self._extract_command_request(mcp_result)

        if request.tool not in runtime.config.sandbox_tools:
            raise MCPCommandPlanError(
                f"MCP server {meta.server_name!r} is not allowed to "
                f"request executable {request.tool!r}"
            )

        result = await asyncio.to_thread(
            self.sandbox_service.execute,
            agent_id,
            request,
        )

        stdout,stdout_truncated = self._truncate_output(result.stdout)
        stderr,stderr_truncated = self._truncate_output(result.stderr)

        if result.timed_out:
            status = "outcome_unknown"
        elif result.exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        return MCPExecutionResult(
            agent_id=agent_id,
            sandbox_id=result.sandbox_id,
            qualified_name=qualified_name,
            executable=request.tool,
            arguments=request.arguments,
            status=status,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _execute_bound_tool_sync(
        self,
        *,
        agent_id: str,
        qualified_name: str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        """
        为当前同步 LangChain Agent提供线程安全桥接

        同步 Agent必须运行在 MCP实践循环之外的工作线程中
        下一轮接入 Orchestrator时使用 asyncio.to_thread(agent.run,...)
        """

        loop = self._event_loop

        if loop is None or not loop.is_running():
            raise MCPClientError("MCPManager is not running")

        if threading.get_ident() == self._event_loop_thread_id:
            raise MCPClientError(
                "synchronous MCP invocation cannot run on the MCP event-loop "
                "thread; use await tool.ainvoke(...) or run the Agent in "
                "asyncio.to_thread(...)"
            )

        future = asyncio.run_coroutine_threadsafe(
            self._execute_bound_tool(
                agent_id=agent_id,
                qualified_name=qualified_name,
                arguments=arguments,
            ),
            loop,
        )

        return future.result()

    def _extract_command_request(self,result: Any) -> CommandRequest:
        text_blocks = [
            block.text
            for block in (getattr(result,"content",None) or [])
            if getattr(block,"type",None) == "text"
            and isinstance(getattr(block,"text",None),str)
        ]

        if bool(getattr(result,"isError",False)):
            message = "\n".join(text_blocks) or "MCP server returned an error"
            raise MCPClientError(message)

        candidates: list[dict[str, Any]] = []

        structured = getattr(result,"structuredContent",None)
        if isinstance(structured,dict):
            candidates.append(structured)

        for text in text_blocks:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed,dict):
                candidates.append(parsed)

        for candidate in candidates:
            plan = candidate.get("sandbox_command")

            # 兼容某些 MCP SDK将函数结果包装在 result字段中的情况
            if plan is None and isinstance(candidate.get("result"), dict):
                plan = candidate["result"].get("sandbox_command")

            if isinstance(plan, dict):
                return self._validate_command_plan(plan)

        raise MCPCommandPlanError(
            "MCP Server did not return a sandbox_command object"
        )

    @staticmethod
    def _validate_command_plan(plan:dict[str,Any]) -> CommandRequest:
        allowed_fields = {"tool","arguments","timeout_seconds"}
        extra_fields = set(plan).difference(allowed_fields)

        if extra_fields:
            raise MCPCommandPlanError(
                f"unsupported sandbox_command fields: {sorted(extra_fields)}"
            )

        tool = plan.get("tool")
        arguments = plan.get("arguments", [])
        timeout_seconds = plan.get("timeout_seconds", 30)

        if not isinstance(tool, str) or not tool.strip():
            raise MCPCommandPlanError(
                "sandbox_command.tool must be a non-empty string"
            )

        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise MCPCommandPlanError(
                "sandbox_command.arguments must be a list of strings"
            )

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
        ):
            raise MCPCommandPlanError(
                "sandbox_command.timeout_seconds must be an integer"
            )

        return CommandRequest(
            tool=tool.strip(),
            arguments=tuple(arguments),
            timeout_seconds=timeout_seconds,
        )

    def _truncate_output(self,output: str) -> tuple[str,bool]:
        if len(output) <= self.output_character_limit:
            return output, False

        half = self.output_character_limit // 2
        omitted = len(output) - (half * 2)

        return (
            output[:half]
            + f"\n...[{omitted} characters omitted]...\n"
            + output[-half:],
            True,
        )

@dataclass(frozen=True,slots=True)
class AgentMCPClient:
    """
    已与单个 Agent和其沙盒绑定的 MCP客户端

    LLM无法修改 agent_id，也无法调用未分配给它的 MCP Tool
    """

    manager: MCPManager
    agent_id: str
    allowed_tools: frozenset[str]

    async def call_tool(
        self,
        qualified_name:str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        self._ensure_allowed(qualified_name)

        return await self.manager._execute_bound_tool(
            agent_id=self.agent_id,
            qualified_name=qualified_name,
            arguments=arguments,
        )

    def call_tool_sync(
        self,
        qualified_name:str,
        arugments:dict[str,Any],
    ) -> MCPExecutionResult:
        self._ensure_allowed(qualified_name)

        return self.manager._execute_bound_tool_sync(
            agent_id=self.agent_id,
            qualified_name=qualified_name,
            arguments=arugments,
        )

    def list_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "qualified_name": meta.qualified_name,
                "description": meta.description,
                "input_schema": meta.input_schema,
                "tags": sorted(meta.tags),
            }
            for meta in self.manager.list_registered_tools()
            if meta.qualified_name in self.allowed_tools
        ]

    def _ensure_allowed(self, qualified_name: str) -> None:
        if qualified_name not in self.allowed_tools:
            raise MCPClientError(
                f"Agent {self.agent_id!r} is not allowed to call "
                f"{qualified_name!r}"
            )

async def build_mcp_runtime(
    sandbox_service: SandboxService,
    registry_path: str | Path | None = None,
) -> MCPManager:
    if registry_path is None:
        registry_path = Path(__file__).with_name("registry.json")

    manager = MCPManager(
        registry=load_registry(registry_path),
        sandbox_service=sandbox_service,
    )

    await manager.start_all()
    return manager