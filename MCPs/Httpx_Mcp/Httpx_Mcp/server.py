# -*- coding: utf-8 -*-


import os
import re
import shutil
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP

# 初始化 MCP 服务实例
mcp = FastMCP("httpx-mcp-server", json_response=True)

# 安全参数校验正则：只允许字母数字及安全符号
# 禁止 shell 元字符（如 ; & | > < ` $ 等），防止命令注入攻击
_FLAG_TOKEN_RE = re.compile(r"^[a-zA-Z0-9\-_=/:.,+%*^]+$")


def _validate_flags(extra_flags: str) -> list[str]:
    """
    解析并校验额外命令行参数的安全性。
    
    Args:
        extra_flags: 空格分隔的参数字符串
        
    Returns:
        安全的参数列表
        
    Raises:
        ValueError: 包含非法字符时抛出
    """
    if not extra_flags:
        return []
    
    tokens = extra_flags.split()
    for token in tokens:
        if not _FLAG_TOKEN_RE.match(token):
            raise ValueError(
                f"参数 '{token}' 包含不允许的字符，仅允许字母数字及 -=/:.,+%*^_ 等安全字符"
            )
    return tokens


def _ensure_httpx() -> str:
    """
    定位 httpx 可执行文件路径。
    
    Returns:
        httpx 完整路径
        
    Raises:
        RuntimeError: httpx 未安装时抛出
    """
    path = shutil.which("httpx")
    if not path:
        raise RuntimeError(
            "httpx 未安装或不在 PATH 中。\n"
            "安装命令: go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest"
        )
    return path


def _run_httpx(
    mode: str,
    target: str,
    extra_flags: str = "-sc -cl -title -tech-detect",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    httpx 执行核心逻辑，支持单目标和批量两种模式。
    
    Args:
        mode: 运行模式，"single"（单目标）或 "batch"（批量）
        target: 目标 URL/IP（single模式）或目标文件路径（batch模式）
        extra_flags: httpx 额外命令行参数，默认开启状态码、内容长度、标题、技术检测
        timeout_seconds: 超时时间（秒）
        
    Returns:
        执行结果字典，包含以下字段：
        - ok: bool，执行是否成功
        - target: str，目标地址或文件路径
        - returncode: int，命令退出码
        - stdout: str，标准输出
        - stderr: str，标准错误
        - command: str，完整命令行
        - timeout: bool（可选），是否超时
        - result_count: int（可选），批量模式结果数量
    """
    # 输入校验：目标不能为空
    if not target or not target.strip():
        return {"ok": False, "error": "target 参数不能为空", "target": target}

    # 批量模式：验证目标文件是否存在
    if mode == "batch" and not os.path.isfile(target):
        return {"ok": False, "error": f"目标文件不存在: {target}", "targets_file": target}

    # 获取 httpx 可执行路径
    httpx_path = _ensure_httpx()

    # 校验额外参数安全性
    try:
        extra_args = _validate_flags(extra_flags)
    except ValueError as e:
        return {"ok": False, "error": str(e), "target": target}

    # 构建命令：使用列表形式避免 shell 注入
    if mode == "single":
        command = [httpx_path, "-u", target] + extra_args
    else:
        command = [httpx_path, "-l", target] + extra_args

    # 执行命令并捕获输出
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "target": target,
            "timeout": True,
            "command": " ".join(command),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "httpx 可执行文件未找到",
            "target": target,
            "command": " ".join(command),
        }
    except PermissionError:
        return {
            "ok": False,
            "error": "没有权限执行 httpx",
            "target": target,
            "command": " ".join(command),
        }
    except OSError as e:
        return {
            "ok": False,
            "error": f"系统错误: {e}",
            "target": target,
            "command": " ".join(command),
        }

    # 构造结果字典
    result: dict[str, Any] = {
        "ok": completed.returncode == 0,
        "target": target,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "command": " ".join(command),
    }

    # 批量模式：统计结果行数
    if mode == "batch" and completed.stdout.strip():
        result["result_count"] = len(completed.stdout.strip().splitlines())

    return result


@mcp.tool()
def run_httpx_probe(
    target: str,
    extra_flags: str = "-sc -cl -title -tech-detect",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    对单个目标 URL 或 IP 执行 HTTP 探测。
    
    Args:
        target: 目标 URL（如 https://example.com）或 IP（如 192.168.1.1）
        extra_flags: 额外命令行参数，默认 "-sc -cl -title -tech-detect"
        timeout_seconds: 超时秒数，默认 60
        
    Returns:
        探测结果字典（见 _run_httpx 返回格式）
    """
    return _run_httpx("single", target, extra_flags, timeout_seconds)


@mcp.tool()
def run_httpx_batch(
    targets_file: str,
    extra_flags: str = "-sc -cl -title -tech-detect",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """
    从文件批量读取目标并执行 HTTP 探测。
    
    Args:
        targets_file: 目标文件路径，每行一个 URL/IP
        extra_flags: 额外命令行参数，默认 "-sc -cl -title -tech-detect"
        timeout_seconds: 超时秒数，默认 120（批量模式时间更长）
        
    Returns:
        探测结果字典（见 _run_httpx 返回格式）
    """
    return _run_httpx("batch", targets_file, extra_flags, timeout_seconds)


def main() -> None:
    """启动 httpx MCP 服务"""
    mcp.run()


if __name__ == "__main__":
    main()
