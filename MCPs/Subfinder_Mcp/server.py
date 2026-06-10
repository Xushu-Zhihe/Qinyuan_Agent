import shutil
import subprocess
from typing import Any
import json
import os
import tempfile

from mcp.server.fastmcp import FastMCP

# 创建 MCP 服务对象。
# json_response=True 表示工具返回结果时尽量使用 JSON 格式，方便客户端解析。
mcp = FastMCP("subfinder-mcp-server", json_response=True)


@mcp.tool(
    title="Check Subfinder",
    description=(
        "Check whether subfinder is installed and available in PATH. "
        "Returns the executable path and version/help output. "
    ),
)
def check_subfinder() -> dict[str, Any]:
    """
    检查本机是否已经安装 subfinder。

    1. shutil.which("subfinder") 会在系统 PATH 环境变量中查找 subfinder 命令。
    2. 如果找不到，说明没有安装，或者安装了但没有配置到 PATH。
    3. 如果找到了，再执行 subfinder -version,用于确认命令可以正常运行。
    """

    # 在系统 PATH 中查找 subfinder 的可执行文件路径。
    subfinder_path = shutil.which("subfinder")

    # 找不到 subfinder 时，直接返回失败信息，不继续执行命令。
    if not subfinder_path:
        return {
            "ok": False,
            "installed": False,
            "message": "subfinder is not installed or not present in PATH",
        }

    # 执行 subfinder -version，获取版本信息。
    completed = subprocess.run(
        [subfinder_path, "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    return {
        "ok": True,
        "installed": True,
        "path": subfinder_path,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


@mcp.tool(
    title="Subfinder Domain Scan",
    description=(
        "Run subfinder against a single domain to discover subdomains. "
        "Input domain should be a root such as example.com. "
        "The tool runs subfinder in silent mode and returns discovered subdomains. "
    ),
)
def run_subfinder_scan(
    domain: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """
    扫描单个根域名的子域名。

    参数说明：
    domain:要扫描的根域名,例如 example.com。
    timeout_seconds:命令最大运行时间,默认 120 秒。

    本函数调用的是 subfinder -d domain -silent。
    -d 表示指定单个域名。
    -silent 表示只输出发现结果，减少无关日志。
    """

    # 每次执行前都重新查找 subfinder，避免路径变化后仍使用旧路径。
    subfinder_path = shutil.which("subfinder")

    if not subfinder_path:
        raise RuntimeError("subfinder is not installed or present in PATH")

    # 拼接要执行的命令。这里使用列表形式，比直接拼字符串更安全。
    command = [
        subfinder_path,
        "-d",
        domain,
        "-silent",
    ]

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
            "domain": domain,
            "timeout": True,
            "command": command,
        }

    # subfinder 的输出通常是一行一个子域名。
    subdomains = []
    lines = completed.stdout.splitlines()

    for i in range(len(lines)):
        line = lines[i].strip()
        if line != "":
            subdomains.append(line)

    return {
        "ok": completed.returncode == 0,
        "domain": domain,
        "count": len(subdomains),
        "subdomains": subdomains,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }


@mcp.tool(
    title="Subfinder JSON Domain Scan",
    description=(
        "Run subfinder against a single domain and return JSON output. "
        "Input domain should be a root domain such as example.com. "
        "The tool runs subfinder with -oJ to get structured JSON lines."
    ),
)
def run_subfinder_json_scan(
    domain: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """
    使用 JSON 格式扫描单个域名的子域名。

    1. 普通扫描只返回文本行。
    2. JSON 扫描使用 -oJ,每一行都是一个 JSON 对象。
    3. 解析 JSON 后，可以从 host 字段中提取子域名。
    """

    subfinder_path = shutil.which("subfinder")

    if not subfinder_path:
        raise RuntimeError("subfinder is not installed or present in PATH")

    # -oJ 表示输出 JSON Lines 格式。
    command = [
        subfinder_path,
        "-d",
        domain,
        "-oJ",
        "-silent",
    ]

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
            "domain": domain,
            "timeout": True,
            "command": command,
        }

    # json_results 保存完整 JSON 结果，subdomains 只保存提取出来的 host。
    json_results = []
    subdomains = []

    lines = completed.stdout.splitlines()

    for i in range(len(lines)):
        line = lines[i].strip()

        if line != "":
            try:
                data = json.loads(line)
                json_results.append(data)

                if "host" in data:
                    subdomains.append(data["host"])
            except json.JSONDecodeError:
                json_results.append(
                    {
                        "raw": line,
                        "parse_error": True,
                    }
                )

    return {
        "ok": completed.returncode == 0,
        "domain": domain,
        "count": len(subdomains),
        "subdomains": subdomains,
        "json_results": json_results,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }


@mcp.tool(
    title="Subfinder Batch Domain Scan",
    description=(
        "Run subfinder against multiple domains. "
        "Input domains should be a list of root domains such as ['example.com', 'test.com']. "
        "The tool writes domains to a temporary file, runs subfinder with -dL, "
        "and returns discovered subdomains."
    ),
)
def run_subfinder_batch_scan(
    domains: list[str],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """
    批量扫描多个域名的子域名。

    参数说明：
    domains:域名列表,例如 ["example.com", "test.com"]。
    timeout_seconds:命令最大运行时间,默认 180 秒。

    subfinder 批量扫描需要使用 -dL 参数。
    -dL 后面跟一个文本文件路径，文件中一行一个根域名。
    所以这里会先创建临时文件，把 domains 写进去，再交给 subfinder 扫描。
    """

    subfinder_path = shutil.which("subfinder")

    if not subfinder_path:
        raise RuntimeError("subfinder is not installed or present in PATH")

    # 清洗输入列表：去掉空格和空字符串，避免写入无效域名。
    clean_domains = []

    for i in range(len(domains)):
        domain = domains[i].strip()
        if domain != "":
            clean_domains.append(domain)

    if len(clean_domains) == 0:
        return {
            "ok": False,
            "message": "domains list is empty",
            "domains": domains,
        }

    # 用来保存临时文件路径，finally 中需要根据它删除临时文件。
    temp_file_path = ""

    try:
        # 创建一个临时 txt 文件，用来存放待扫描域名。
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            suffix=".txt",
            encoding="utf-8",
        ) as temp_file:
            temp_file_path = temp_file.name

            # 一行写入一个域名，这是 subfinder -dL 需要的格式。
            for i in range(len(clean_domains)):
                temp_file.write(clean_domains[i] + "\n")

        command = [
            subfinder_path,
            "-dL",
            temp_file_path,
            "-silent",
        ]

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        subdomains = []
        lines = completed.stdout.splitlines()

        for i in range(len(lines)):

            line = lines[i].strip()
            if line != "":
                subdomains.append(line)

        return {
            "ok": completed.returncode == 0,
            "domains": clean_domains,
            "domain_count": len(clean_domains),
            "count": len(subdomains),
            "subdomains": subdomains,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "command": command,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "domains": clean_domains,
            "timeout": True,
            "timeout_seconds": timeout_seconds,
        }

    finally:
        # 无论扫描成功、失败还是超时，都删除临时文件。
        if temp_file_path != "" and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def main() -> None:
    """
    MCP 服务启动入口。

    当直接运行 uv run server.py 时，会执行 main()，从而启动 MCP 服务。
    """
    mcp.run()

if __name__ == "__main__":
    main()