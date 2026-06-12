"""
FFUF MCP Server - 基于 ffuf 的 Web 模糊测试 MCP 服务器
提供目录发现、API 端点发现、隐藏路径发现功能
"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import asyncio
import json
import os
import sys
import tempfile
import uuid


# 根据操作系统选择 ffuf 命令
FFUF_CMD = "ffuf.exe" if sys.platform.startswith("win") else "ffuf"

# 创建 MCP 服务器实例
server = Server("ffuf-mcp")


def parse_ffuf_output(output: str) -> list[dict]:
    """
    解析 ffuf 的 JSON 输出
    将 ffuf 返回的 JSON 结果转换为标准字典格式
    """
    results = []
    try:
        data = json.loads(output)
        for item in data.get("results", []):
            results.append({
                "url": item.get("url", ""),
                "status": item.get("status", 0),
                "length": item.get("length", 0),
                "words": item.get("words", 0),
                "lines": item.get("lines", 0),
                "content-type": item.get("content-type", "")
            })
    except json.JSONDecodeError:
        pass
    return results


async def run_ffuf(args: list) -> list[dict]:
    """
    异步执行 ffuf 命令并返回解析后的结果
    使用临时文件存储 JSON 输出，避免日志干扰
    """
    try:
        # 使用临时文件存储 JSON 输出，Windows 兼容
        output_file = os.path.join(tempfile.gettempdir(), f"ffuf_{uuid.uuid4().hex}.json")

        # 添加输出参数
        ffuf_args = args + ["-of", "json", "-o", output_file]

        # 创建子进程执行 ffuf
        proc = await asyncio.create_subprocess_exec(
            FFUF_CMD,
            *ffuf_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        # 从临时文件读取 JSON 结果
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return parse_ffuf_results(data)
        except (FileNotFoundError, json.JSONDecodeError):
            # 回退：尝试解析 stdout
            if stdout:
                return parse_ffuf_output(stdout.decode())
            return []
        finally:
            # 清理临时文件
            try:
                os.unlink(output_file)
            except:
                pass
    except FileNotFoundError:
        raise Exception("ffuf 未找到，请先安装 ffuf")
    except Exception as e:
        raise Exception(f"ffuf 执行错误: {str(e)}")


def parse_ffuf_results(data: dict) -> list[dict]:
    """解析 ffuf JSON 文件格式的结果"""
    results = []
    for item in data.get("results", []):
        results.append({
            "url": item.get("url", ""),
            "status": item.get("status", 0),
            "length": item.get("length", 0),
            "words": item.get("words", 0),
            "lines": item.get("lines", 0),
            "content-type": item.get("content-type", "")
        })
    return results


@server.list_tools()
async def list_tools() -> list[Tool]:
    """
    列出所有可用的工具
    MCP 协议要求实现 list_tools 回调
    """
    return [
        # 目录发现工具
        Tool(
            name="discover_directories",
            description="使用 ffuf 发现 Web 服务器上的目录",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标 URL，需包含 FUZZ 关键字 (如 http://example.com/FUZZ)"},
                    "wordlist": {"type": "string", "description": "字典文件路径"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "要尝试的文件扩展名"},
                    "rate": {"type": "number", "description": "每秒请求数", "default": 100}
                },
                "required": ["target", "wordlist"]
            }
        ),
        # API 端点发现工具
        Tool(
            name="discover_api_endpoints",
            description="使用 ffuf 发现 API 端点",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标 URL，需包含 FUZZ 关键字"},
                    "wordlist": {"type": "string", "description": "API 字典路径"},
                    "methods": {"type": "array", "items": {"type": "string"}, "description": "要测试的 HTTP 方法", "default": ["GET"]},
                    "rate": {"type": "number", "description": "每秒请求数", "default": 100}
                },
                "required": ["target", "wordlist"]
            }
        ),
        # 隐藏路径发现工具
        Tool(
            name="discover_hidden_paths",
            description="发现隐藏路径、备份文件和其他敏感文件",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标 URL，需包含 FUZZ 关键字"},
                    "wordlist": {"type": "string", "description": "字典路径"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "要尝试的扩展名 (默认: bak,old,backup)"},
                    "rate": {"type": "number", "description": "每秒请求数", "default": 100}
                },
                "required": ["target", "wordlist"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    处理工具调用请求
    根据工具名称调用相应的 ffuf 命令
    """
    try:
        target = arguments["target"]
        wordlist = arguments["wordlist"]
        rate = arguments.get("rate", 100)

        if name == "discover_directories":
            # 目录发现
            args = ["-u", target, "-w", wordlist, "-rate", str(rate),
                    "-mc", "200,204,301,302,307,308,401,403,500"]
            if "extensions" in arguments and arguments["extensions"]:
                args.extend(["-e", ",".join(arguments["extensions"])])
            results = await run_ffuf(args)

        elif name == "discover_api_endpoints":
            # API 端点发现 - ffuf -X 只支持单个方法，需对每个方法单独执行
            methods = arguments.get("methods", ["GET"])
            all_results = []
            for method in methods:
                args = ["-u", target, "-w", wordlist, "-rate", str(rate), "-X", method.upper(),
                        "-mc", "200,201,204,301,302,307,308,401,403,405,500"]
                results = await run_ffuf(args)
                for r in results:
                    r["method"] = method.upper()
                all_results.extend(results)
            # 去重
            seen = set()
            results = []
            for r in all_results:
                key = (r["url"], r["method"])
                if key not in seen:
                    seen.add(key)
                    results.append(r)

        elif name == "discover_hidden_paths":
            # 隐藏路径发现
            args = ["-u", target, "-w", wordlist, "-rate", str(rate),
                    "-mc", "200,204,301,302,307,308,401,403,500"]
            exts = arguments.get("extensions") if "extensions" in arguments and arguments["extensions"] else ["bak", "old", "backup", "swp", "tmp", "log", "conf", "config"]
            args.extend(["-e", ",".join(exts)])
            results = await run_ffuf(args)

        else:
            raise ValueError(f"未知工具: {name}")

        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


async def main():
    """
    主入口函数
    启动 MCP 服务器并通过 stdio 通信
    """
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
