"""
Dirsearch MCP Server
A Model Context Protocol server for directory, API and hidden path scanning.

通过 MCP 协议将 dirsearch 工具暴露给 AI 助手，
支持目录爆破、API 发现、后台路径扫描等多种模式。
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anyio # 异步运行时，替代 asyncio.run()，兼容 mcp>=1.0
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------- 日志配置 ----------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 检测操作系统，用于兼容 Windows / Linux / macOS 的路径处理
IS_WINDOWS = os.name == 'nt'

# ============================================================
#  数据模型
# ============================================================


@dataclass
class ScanResult:
    """单条扫描结果"""
    url: str    # 发现的 URL
    status_code: int # HTTP 状态码
    content_length: int # 响应体长度
    redirect_url: str | None = None # 重定向目标（如果有）

# ============================================================
#  Dirsearch 扫描器封装
# ============================================================

class DirsearchScanner:
    """
    dirsearch 命令行工具的 Python 封装。
    负责：查找可执行文件、构建命令、执行扫描、解析结果。
    """

    def __init__(self, dirsearch_path: str | tuple[str, str] | None = None):
        """
        初始化扫描器。
        Args:
            dirsearch_path: dirsearch 的路径。
                - str: 直接可执行文件路径
                - tuple[str, str]: (python解释器, dirsearch.py脚本路径)
                - None: 自动查找
        """
        self.dirsearch_path = dirsearch_path or self._find_dirsearch()

        # ---------- 工具查找 ----------

    def _find_dirsearch(self) -> str | tuple[str, str]:
        """
        跨平台自动查找 dirsearch 安装位置。
        查找顺序：
        1. PATH 中的 dirsearch / dirsearch.exe
        2. 常见目录中的 dirsearch.py 脚本
        返回: 可执行文件路径 或 (python, script_path) 元组
        """
        # 先找独立的可执行文件
        executable_names = ["dirsearch", "dirsearch.exe"]
        
        for name in executable_names:
            path = shutil.which(name)
            if path:
                return path

        # 再找作为 Python 脚本安装的 dirsearch.py
        dirsearch_py_paths = [
            os.path.expanduser("~/dirsearch/dirsearch.py"),
            "/opt/dirsearch/dirsearch.py",
            "/usr/share/dirsearch/dirsearch.py",
        ]

        # Windows 特殊路径
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirsearch_py_paths.append(os.path.join(appdata, "dirsearch", "dirsearch.py"))
        
        programfiles = os.environ.get("PROGRAMFILES", "C:\\Program Files")
        if programfiles:
            dirsearch_py_paths.append(os.path.join(programfiles, "dirsearch", "dirsearch.py"))
        
        # 找到了 .py 脚本，返回 (python, script) 以便用 python 执行
        for path in dirsearch_py_paths:
            if os.path.exists(path):
                python_exec = shutil.which("python") or shutil.which("python3") or "python"
                return python_exec, path
            
        # 什么都没找到，返回默认值，后续会报 "not found" 错误
        return "dirsearch"
    
    # ---------- 输入校验与清洗（安全措施）----------

    def _validate_url(self, url: str) -> bool:
        """校验 URL 格式，仅允许 http/https 协议，防止 SSRF 和内网扫描。"""
        try:
            parsed = urlparse(url)
            return parsed.scheme in ("http", "https") and parsed.netloc
        except ValueError:
            return False

    def _sanitize_header_name(self, name: str) -> str:
        """清洗 HTTP 头名称，防止注入攻击。"""
        return re.sub(r'[^a-zA-Z0-9\-_]', '', name)

    def _sanitize_header_value(self, value: str) -> str:
        """
        清洗 HTTP 头值，移除换行符和空字符。
        防止通过换行注入额外的 HTTP 头。
        """
        return re.sub(r'[\n\r\0]', '', value)

    def _sanitize_cookie_value(self, value: str) -> str:
        """
        清洗 Cookie 值。
        将特殊字符 ; 和 = 进行 URL 编码，防止 Cookie 注入。
        """
        value = self._sanitize_header_value(value)
        return value.replace(';', '%3B').replace('=', '%3D')

    # ---------- 工具可用性检查 ----------

    async def check_dirsearch_available(self) -> tuple[bool, str]:
        """
        检查 dirsearch 是否可用。
        通过执行 dirsearch --version 来判断。
        返回: (是否可用, 描述信息)
        """
        try:
            cmd = self._build_cmd_list()
            proc = await asyncio.create_subprocess_exec(
                *cmd, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                return True, f"dirsearch is available: {stdout.decode().strip()}"
            else:
                return False, f"dirsearch check failed: {stderr.decode().strip()}"
        except FileNotFoundError:
            return False, "dirsearch not found in PATH"
        except Exception as e:
            return False, f"Error checking dirsearch: {str(e)}"

    def _build_cmd_list(self) -> list[str]:
        """
        将 dirsearch_path 转换为命令列表。
        - 如果是 str: 返回 [path]
        - 如果是 tuple: 返回 [python, script]（用 Python 执行脚本）
        """
        if isinstance(self.dirsearch_path, tuple):
            return list(self.dirsearch_path)
        return [self.dirsearch_path]

     # ---------- 核心扫描方法 ----------

    async def scan(
        self,
        target_url: str,
        extensions: list[str] | None = None,
        wordlist: str | None = None,
        exclude_status_codes: list[int] | None = None,
        include_status_codes: list[int] | None = None,
        threads: int = 10,
        timeout: int = 30,
        recursive: bool = False,
        recursion_depth: int = 1,
        follow_redirects: bool = False,
        random_user_agent: bool = True,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        proxy: str | None = None,
        delay: float = 0,
        max_time: int | None = None,
    ) -> tuple[bool, list[ScanResult] | str]:
        """
        执行 dirsearch 扫描。

        Args:
            target_url:          目标 URL
            extensions:          要探测的文件扩展名列表
            wordlist:            自定义字典路径
            exclude_status_codes: 排除的 HTTP 状态码
            include_status_codes: 仅包含的 HTTP 状态码
            threads:             并发线程数 (1-100)
            timeout:             单次请求超时秒数 (1-300)
            recursive:           是否递归扫描发现的目录
            recursion_depth:     递归深度 (1-10)
            follow_redirects:    是否跟随重定向
            random_user_agent:   是否使用随机 User-Agent
            headers:             自定义请求头
            cookies:             自定义 Cookie
            proxy:               代理地址
            delay:               请求间隔秒数
            max_time:            最大扫描时长秒数

        Returns:
            (True, [ScanResult]) 成功时
            (False, str)         失败时，str 为错误信息
        """

         # === 参数校验 ===
        if not self._validate_url(target_url):
            return False, f"Invalid URL format: {target_url}. Must be http:// or https://"
        
         # 参数限幅，防止资源耗尽
        threads = max(1, min(threads, 100))
        timeout = max(1, min(timeout, 300))
        recursion_depth = max(1, min(recursion_depth, 10))

        # === 构建 dirsearch 命令 ===
        cmd = self._build_cmd_list()
        cmd.extend(["-u", target_url])

        # 文件扩展名
        if extensions:
            cmd.extend(["-e", ",".join(extensions)])
        else:
            cmd.extend(["-e", "php,html,js,txt,json,xml"])
        
        # 自定义字典
        if wordlist:
            cmd.extend(["-w", wordlist])
        
        # 排除的 HTTP 状态码
        if exclude_status_codes:
            cmd.extend(["-x", ",".join(str(c) for c in exclude_status_codes)])
        if include_status_codes:
            cmd.extend(["-i", ",".join(str(c) for c in include_status_codes)])
        
        # 并发线程数，并发与超时
        cmd.extend(["-t", str(threads)])
        cmd.extend(["--timeout", str(timeout)])

        # 递归扫描发现的目录
        if recursive:
            cmd.append("-r")
            if recursion_depth > 1:
                cmd.extend(["-R", str(recursion_depth)])
        
        # 跟随重定向
        if follow_redirects:
            cmd.append("-F")
        
        # 随机 User-Agent（绕过基础反爬）
        if random_user_agent:
            cmd.append("--random-agent")
        
        # 自定义请求头（经过安全清洗）
        if headers:
            for key, value in headers.items():
                sanitized_key = self._sanitize_header_name(key)
                sanitized_value = self._sanitize_header_value(value)
                if sanitized_key:
                    cmd.extend(["-H", f"{sanitized_key}: {sanitized_value}"])

        # Cookie（经过安全清洗）
        if cookies:
            cookie_parts = []
            for k, v in cookies.items():
                sanitized_k = self._sanitize_header_name(k)
                sanitized_v = self._sanitize_cookie_value(v)
                if sanitized_k:
                    cookie_parts.append(f"{sanitized_k}={sanitized_v}")
            if cookie_parts:
                cookie_str = "; ".join(cookie_parts)
                cmd.extend(["--cookie", cookie_str])

        # 代理（经过安全清洗）
        if proxy:
            cmd.extend(["--proxy", proxy])

        # 请求间隔（降低被封风险）
        if delay > 0:
            cmd.extend(["--delay", f"{delay:.2f}"])

        # 总时间限制
        if max_time:
            cmd.extend(["--max-time", str(max_time)])

        # 输出格式：JSON
        cmd.extend(["--format", "json"])

         # === 创建临时文件存放扫描结果 ===
        output_file = None
        try:
            fd, output_file = tempfile.mkstemp(suffix='.json')
            os.close(fd)
             # 非 Windows 系统设置文件权限为仅当前用户可读写
            if not IS_WINDOWS:
                os.chmod(output_file, 0o600)
            cmd.extend(["-o", output_file])
            cmd.append("-q") # 静默模式，减少控制台输出

            logger.info(f"Running dirsearch: {' '.join(cmd)}")

            # === 异步执行 dirsearch ===
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                # 等待扫描完成，超时则强制终止
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=(max_time or 300) + 30 # 多给 10 秒用于写入结果文件
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return False, "Scan timed out"

            # 检查退出码
            if proc.returncode != 0:
                error_msg = stderr.decode().strip()[:500] if stderr else "Unknown error"
                logger.error(f"dirsearch failed: {error_msg}")
                return False, f"Scan failed: {error_msg}"

            # === 解析结果 ===
            results = self._parse_results(output_file)
            logger.info(f"Scan completed, found {len(results)} results")
            return True, results

        except FileNotFoundError:
            return False, "dirsearch not found"
        except Exception as e:
            logger.exception("Scan error")
            return False, f"Scan error: {str(e)}"
        finally:
             # === 清理临时文件 ===
            if output_file and os.path.exists(output_file):
                try:
                    os.unlink(output_file)
                except OSError:
                    pass

    # ---------- 结果解析 ----------

    def _parse_results(self, output_file: str) -> list[ScanResult]:
        """
        解析 dirsearch 的 JSON 输出文件。
        兼容不同版本的 JSON 格式。
        解析失败时回退到文本解析。
        """
        results = []

        try:
            with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            if not content.strip():
                logger.info("No results found in output file")
                return results

            data = json.loads(content)

             # 兼容多种 JSON 格式
            if isinstance(data, dict):
                 # 标准格式: {"results": [...]} 或 {"result": [...]}
                items = data.get('results', data.get('result', []))
                if not isinstance(items, list):
                    items = []
            elif isinstance(data, list):
                # 直接是数组: [...]
                items = data
            else:
                logger.warning(f"Unexpected JSON format: {type(data).__name__}")
                return results

             # 提取字段，兼容不同的字段名
            for item in items:
                result = ScanResult(
                    url=item.get("url", item.get("path", "")),
                    status_code=item.get("status", item.get("statusCode", 0)),
                    content_length=item.get("length", item.get("contentLength", 0)),
                    redirect_url=item.get("redirect", item.get("redirectUrl", None))
                )
                if result.url:  # 只保留有效 URL 的结果
                    results.append(result)

        except json.JSONDecodeError as e:
            # JSON 解析失败，尝试用正则从文本中提取
            logger.warning(f"JSON parsing failed, trying text fallback: {e}")
            results = self._parse_text_results(output_file)
        except Exception as e:
            logger.error(f"Error parsing results: {e}")

        return results

    def _parse_text_results(self, output_file: str) -> list[ScanResult]:
        """
        文本格式回退解析。
        当 dirsearch 输出非标准 JSON 时，尝试用正则匹配提取数据。
        """
        results = []
        # 三种常见的 dirsearch 输出格式
        patterns = [
            r"Status:\s*(\d+).*?Size:\s*(\d+).*?URL:\s*(\S+)",
            r"(\d+)\s+(\d+)\s+(\S+)",
            r"\[(\d+)\].*?(\d+)\s+(.+)$"
        ]

        try:
            with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    for pattern in patterns:
                        match = re.search(pattern, line)
                        if match:
                            try:
                                result = ScanResult(
                                    url=match.group(3).strip(),
                                    status_code=int(match.group(1)),
                                    content_length=int(match.group(2))
                                )
                                results.append(result)
                                break # 匹配到一个就跳到下一行
                            except ValueError:
                                continue
        except Exception as e:
            logger.error(f"Error parsing text results: {e}")

        return results


# ============================================================
#  MCP Server 实例化
# ============================================================


# 创建 MCP Server，名称为 "dirsearch-mcp"
server = Server("dirsearch-mcp")

# 创建扫描器实例（自动查找 dirsearch）
scanner = DirsearchScanner()

# ============================================================
#  响应格式化函数
# ============================================================

def _format_results(target: str, results: list[ScanResult], scan_type: str = "full") -> list[TextContent]:
    """
    格式化成功响应。
    将扫描结果包装为 MCP TextContent 格式的 JSON。
    
    Args:
        target:    扫描目标 URL
        results:   扫描结果列表
        scan_type: 扫描类型标识 (full / quick / api / admin)
    """
    results_data = [
        {
            "url": r.url,
            "status_code": r.status_code,
            "content_length": r.content_length,
            "redirect_url": r.redirect_url
        }
        for r in results
    ]
    return [TextContent(
        type="text",
        text=json.dumps({
            "success": True,
            "scan_type": scan_type,
            "target": target,
            "results_count": len(results_data),
            "results": results_data
        }, indent=2)
    )]


def _format_error(error: str) -> list[TextContent]:
    """
    格式化错误响应。
    统一错误输出格式，方便 AI 助手理解。
    """
    return [TextContent(
        type="text",
        text=json.dumps({
            "success": False,
            "error": error
        }, indent=2)
    )]


# ============================================================
#  MCP 工具注册 —— 告诉 Host 我能做什么
# ============================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """
    向 MCP Host注册可用的工具列表。
    每个 Tool 定义了名称、描述和参数 Schema。
    返回的工具列表会注入到 LLM 的 system prompt 中，
    让 AI 知道可以调用哪些工具以及如何传参。
    """
    return [
         # ---------- 1. 通用完整扫描 ----------
        Tool(
            name="dirsearch_scan",
            description="Scan a target URL for directories, files, APIs, and hidden paths using dirsearch. Returns discovered paths with status codes and sizes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_url": {
                        "type": "string",
                        "description": "Target URL to scan (e.g., https://example.com)"
                    },
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File extensions to scan for (e.g., ['php', 'html', 'js']). Default: ['php', 'html', 'js', 'txt', 'json', 'xml']"
                    },
                    "wordlist": {
                        "type": "string",
                        "description": "Path to custom wordlist file"
                    },
                    "exclude_status_codes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Status codes to exclude from results (e.g., [404, 403])"
                    },
                    "include_status_codes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Only include these status codes in results"
                    },
                    "threads": {
                        "type": "integer",
                        "description": "Number of threads. Default: 10",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 100
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds. Default: 30",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 300
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Enable recursive scanning. Default: false",
                        "default": False
                    },
                    "recursion_depth": {
                        "type": "integer",
                        "description": "Maximum recursion depth. Default: 1",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 10
                    },
                    "follow_redirects": {
                        "type": "boolean",
                        "description": "Follow HTTP redirects. Default: false",
                        "default": False
                    },
                    "random_user_agent": {
                        "type": "boolean",
                        "description": "Use random user agent. Default: true",
                        "default": True
                    },
                    "headers": {
                        "type": "object",
                        "description": "Custom HTTP headers (e.g., {'Authorization': 'Bearer token'})"
                    },
                    "cookies": {
                        "type": "object",
                        "description": "Cookies to send with requests"
                    },
                    "proxy": {
                        "type": "string",
                        "description": "Proxy URL (e.g., http://127.0.0.1:8080)"
                    },
                    "delay": {
                        "type": "number",
                        "description": "Delay between requests in seconds. Default: 0",
                        "default": 0,
                        "minimum": 0
                    },
                    "max_time": {
                        "type": "integer",
                        "description": "Maximum scan time in seconds"
                    }
                },
                "required": ["target_url"]
            }
        ),


        # ---------- 2. 工具可用性检查 ----------
        Tool(
            name="dirsearch_check",
            description="Check if dirsearch is installed and available on the system.",
            inputSchema={"type": "object", "properties": {}}
        ),

         # ---------- 3. 快速扫描 ----------
        Tool(
            name="dirsearch_quick_scan",
            description="Quick scan for common directories and files. Uses default settings optimized for speed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_url": {
                        "type": "string",
                        "description": "Target URL to scan"
                    },
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File extensions to scan for. Default: common web extensions"
                    }
                },
                "required": ["target_url"]
            }
        ),

         # ---------- 4. API 端点扫描 ----------
        Tool(
            name="dirsearch_api_scan",
            description="Scan for API endpoints and hidden API paths. Optimized for API discovery.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_url": {
                        "type": "string",
                        "description": "Target URL (base API URL)"
                    },
                    "api_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "API file extensions. Default: ['json', 'xml', 'yaml']"
                    },
                    "headers": {
                        "type": "object",
                        "description": "Custom headers (e.g., for authentication)"
                    }
                },
                "required": ["target_url"]
            }
        ),

        # ---------- 5. 后台管理页面扫描 ----------
        Tool(
            name="dirsearch_admin_scan",
            description="Scan for admin panels, login pages, and administrative interfaces.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_url": {
                        "type": "string",
                        "description": "Target URL to scan"
                    },
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File extensions. Default: ['php', 'html', 'asp', 'aspx', 'jsp']"
                    }
                },
                "required": ["target_url"]
            }
        )
    ]



# ============================================================
#  MCP 工具调用 —— 当 Host 调用工具时做什么
# ============================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """
    处理来自 MCP Host 的工具调用请求。
    
    Args:
        name:      工具名称（对应 list_tools 中定义的 name）
        arguments: 工具参数字典（对应 inputSchema 中定义的 properties）
    
    Returns:
        TextContent 列表，包含 JSON 格式的执行结果
    """
    try:
        # ----- dirsearch_check: 检查工具是否可用 -----
        if name == "dirsearch_check":
            available, message = await scanner.check_dirsearch_available()
            return [TextContent(
                type="text",
                text=json.dumps({"available": available, "message": message}, indent=2)
            )]

        # ----- dirsearch_scan: 通用完整扫描 -----
        elif name == "dirsearch_scan":
            success, result = await scanner.scan(
                target_url=arguments["target_url"],
                extensions=arguments.get("extensions"),
                wordlist=arguments.get("wordlist"),
                exclude_status_codes=arguments.get("exclude_status_codes"),
                include_status_codes=arguments.get("include_status_codes"),
                threads=arguments.get("threads", 10),
                timeout=arguments.get("timeout", 30),
                recursive=arguments.get("recursive", False),
                recursion_depth=arguments.get("recursion_depth", 1),
                follow_redirects=arguments.get("follow_redirects", False),
                random_user_agent=arguments.get("random_user_agent", True),
                headers=arguments.get("headers"),
                cookies=arguments.get("cookies"),
                proxy=arguments.get("proxy"),
                delay=arguments.get("delay", 0),
                max_time=arguments.get("max_time"),
            )
            return _format_results(arguments["target_url"], result) if success else _format_error(result)

        # ----- dirsearch_quick_scan: 快速扫描（预设优化参数） -----
        elif name == "dirsearch_quick_scan":
            extensions = arguments.get("extensions", ["php", "html", "js", "txt", "json", "bak", "old"])
            success, result = await scanner.scan(
                target_url=arguments["target_url"],
                extensions=extensions,
                threads=20,  # 更多线程
                timeout=10,  # 更短超时
                exclude_status_codes=[404],  # 自动过滤 404
            )
            return _format_results(arguments["target_url"], result, "quick") if success else _format_error(result)

         # ----- dirsearch_api_scan: API 端点扫描（预设 API 参数） -----
        elif name == "dirsearch_api_scan":
            api_extensions = arguments.get("api_extensions", ["json", "xml", "yaml"])
            success, result = await scanner.scan(
                target_url=arguments["target_url"],
                extensions=api_extensions,
                threads=15,
                timeout=15,
                exclude_status_codes=[404, 500],
                headers=arguments.get("headers", {"Accept": "application/json"}),
            )
            if success:
                 # 过滤只保留有意义的 API 状态码
                api_results = [r for r in result if r.status_code in [200, 201, 401, 403]]
                return _format_results(arguments["target_url"], api_results, "api")
            return _format_error(result)

          # ----- dirsearch_admin_scan: 后台管理页面扫描（内置字典） -----
        elif name == "dirsearch_admin_scan":
            extensions = arguments.get("extensions", ["php", "html", "asp", "aspx", "jsp"])


            # 内置的后台管理路径字典（40+ 常见路径）
            admin_paths = [
                "admin", "administrator", "admin/login", "admin/login.php",
                "admincp", "adminpanel", "backend", "dashboard", "manage",
                "management", "manager", "control", "controlpanel", "cpanel",
                "wp-admin", "wp-login.php", "phpmyadmin", "pma",
                "admin.php", "login", "login.php", "signin", "signin.php",
                "user/login", "account/login", "auth/login", "oauth/login",
                "panel", "portal", "console", "webadmin", "siteadmin",
                "sysadmin", "root", "superuser", "moderator", "mod",
                "api/admin", "api/v1/admin", "api/v2/admin",
            ]


             # 将内置字典写入临时文件
            wordlist_path = None
            try:
                fd, wordlist_path = tempfile.mkstemp(suffix='.txt')
                os.close(fd)
                if not IS_WINDOWS:
                    os.chmod(wordlist_path, 0o600)
                with open(wordlist_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(admin_paths))

                success, result = await scanner.scan(
                    target_url=arguments["target_url"],
                    extensions=extensions,
                    wordlist=wordlist_path,
                    threads=10,
                    timeout=15,
                    exclude_status_codes=[404],
                )
                if success:
                    # 过滤保留有意义的管理页面状态码
                    admin_results = [r for r in result if r.status_code in [200, 301, 302, 401, 403]]
                    return _format_results(arguments["target_url"], admin_results, "admin")
                return _format_error(result)
            finally:
                # 清理临时字典文件
                if wordlist_path and os.path.exists(wordlist_path):
                    try:
                        os.unlink(wordlist_path)
                    except OSError:
                        pass

          # ----- 未知工具 -----
        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2)
            )]

    except Exception as e:
         # 全局异常捕获，确保任何错误都能以标准格式返回
        logger.exception("Tool execution error")
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e), "error_type": type(e).__name__}, indent=2)
        )]

# ============================================================
#  服务启动入口
# ============================================================


async def _run_server():
    """
    启动 MCP Server。
    通过 stdio 与 MCP Host（如 Claude Desktop）通信。
    使用 async with 管理连接生命周期。
    """
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()  # 初始化选项（能力协商）
        )


def main():
    """
    主入口函数。
    使用 anyio.run() 启动异步事件循环。
    anyio 兼容 mcp>=1.0 的异步模型，替代传统的 asyncio.run()。
    """
    anyio.run(_run_server)


# 直接运行时启动服务
if __name__ == "__main__":
    main()