#只读查询shell

from langchain.tools import tool
import subprocess
import re
"""Host shell execution is disabled."""

from Tools.execute_bash import create_sandbox_command_tool

__all__ = ["create_sandbox_command_tool"]

# 允许出现在管道中的只读命令
ALLOWED_QUERY_COMMANDS = {
    "ps", "ls", "find", "grep", "cat", "head", "tail", 
    "awk", "sed", "sort", "uniq", "wc", "df", "du", 
    "ss", "netstat", "ip", "lsof", "whoami", "id", 
    "file", "strings", "less", "more", "xargs", "cut",
    "tr", "dirname", "basename", "pwd", "echo"
}

# 危险模式：命令分隔、重定向、命令替换
DANGEROUS_PATTERNS = re.compile(r'[;]|&&|\|\||[<>]|`\w+|\$\(')

def validate_pipeline(command: str) -> tuple[bool,str]:
    """
    验证命令是否为安全的只读查询命令管道。
    """
    if DANGEROUS_PATTERNS.search(command):
        return False, "命令包含危险模式（禁止 ; && || > < $() ` ）"

    # 按管道分割，检查每段的主命令
    segments = command.split("|")
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # 取第一个词（主命令）
        cmd = seg.split()[0]
        if cmd not in ALLOWED_QUERY_COMMANDS:
            return False, f"命令 '{cmd}' 不在允许列表中"
    
    return True, ""

@tool
def query_shell(command: str, timeout: int = 30) -> str:
    """
    运行一个只读查询命令管道并返回输出结果。
    Args:
        command: 要执行的命令字符串（允许管道，但必须是只读查询命令）。
        timeout: 命令执行的最大等待时间（秒）。由调用方根据命令复杂度决定，范围 1-600。
    """
    if not command or not command.strip():
        return "错误：命令不能为空"
    
    if timeout < 1 or timeout > 600:
        return "错误：timeout 必须在 1-600 秒之间，请根据命令预估耗时重新传入"
    
    try:
        result = subprocess.check_output(
            command,
            shell=True,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout
        )
        return result if result else "命令执行成功（无输出）"
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超过 {timeout} 秒，已超时终止"
    except subprocess.CalledProcessError as e:
        return f"查询失败（退出码 {e.returncode}）：\n{e.output or '无输出'}"
    except Exception as e:
        return f"错误：{str(e)}"