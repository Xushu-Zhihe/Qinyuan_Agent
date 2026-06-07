# 定义 server 配置模型，读取 registry 文件，做配置检验
# 让manager 只拿到干净的 MCPRegisterConfig 
from __future__ import annotations

from dataclasses import dataclass, field
# 引入 dataclass 和 field dataclass：定义轻量数据模型,
# field(default_factory=...) 安全地创建默认 list/dict
from pathlib import Path
import json #用来解析 registry.json

@dataclass(slots=True)#slots=True: 限制对象只能有声明过的字段
class MCPServerConfig: #定义单个 MCP server 的配置模型，
    name: str
    command: str #启动 MCP server 的命令
    #传给 command 的命令行参数列表
    args: list[str] = field(default_factory=list) #传给 command 的参数列表
    env:dict[str,str] = field(default_factory=dict)# server 启动时附加的环境变量
    tags:list[str] = field(default_factory=list)
    enabled:bool = True #是否启用这个 server 

@dataclass(slots=True)# 定义整个 registry 配置模型
class MCPRegistryConfig:
    servers:dict[str,MCPServerConfig]

#读取配置文件的主函数
def load_registry(path:str | Path) -> MCPRegistryConfig:
    #把传进来的路径转成 Path,读取文件内容，再解析JSON
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    #读取 JSON 里的 mcpServers 字段
    raw_servers = raw.get("mcpServers")
    #校验 mcpServers必须是对象
    if not isinstance(raw_servers,dict):
        raise ValueError("registry must contain an object field named 'mcpServers'")
    #创建最终返回的 server配置字典
    servers:dict[str,MCPServerConfig] = {}
    
    #遍历每个 server
    for name,item in raw_servers.items():
        #校验每个 server的配置必须是对象
        if not isinstance(item,dict):
            raise ValueError(f"server config for {name!r} must be an object")

        command = item.get("command")
        args = item.get("args",[])
        env = item.get("env",{})
        tags = item.get("tags",[])    
        enabled = item.get("enabled",True)
        #校验command 必须是非空字符串
        if not isinstance(command,str) or not command:
            raise ValueError(f"{name!r}.command must be a non-empty string")
        #校验args 必须是字符串列表
        if not isinstance(args,list) or not all(isinstance(x,str) for x in args):
            raise ValueError(f"{name!r}.args must be a list[str]")
        #校验 env必须是 dict[str,str]
        if not isinstance(env,dict) or not all(isinstance(k,str) and isinstance(v,str)
        for k,v in env.items()):
            raise ValueError(f"{name!r}.tags must be a dict[str,str]")
        #校验 tags必须是字符串列表
        if not isinstance(tags,list) or not all(isinstance(x,str) for x in tags):
            raise ValueError(f"{name!r}.tags must be a list[str]")
        #校验 enabled必须是布尔值，json里写true/false
        if not isinstance(enabled,bool):
            raise ValueError(f"{name!r}.enabled must be a bool")
        # 原始dict 转化成强结构的MCPServerConfig 对象
        servers[name] = MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            tags=tags,
            enabled=enabled,
        )
    # 返回完整 registry 配置
    return MCPRegistryConfig(servers=servers)