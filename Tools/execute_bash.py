#bash执行

from langchain.tools import tool
import subprocess

@tool
def execute_bash(command: str, timeout: int = 60) -> str:
    """
    执行 Bash 命令，主要用于 Skills 中调用脚本。
    Skills的目录在 /Skills/下

    Args:
        command: 要执行的命令
        timeout: 超时时间（秒），由调用方根据脚本预估耗时决定，范围 1-600
    """
    if not command or not command.strip():
        return "错误：命令不能为空"

    if timeout < 1 or timeout > 600:
        return "错误：timeout 必须在 1-600 秒之间，请根据命令预估耗时重新传入"

    try:
        result = subprocess.check_output(
            command, shell=True, stderr=subprocess.STDOUT, text=True, timeout=timeout
        )
        return result if result else "执行成功（无输出）"
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超过 {timeout} 秒，已超时终止"
    except subprocess.CalledProcessError as e:
        return f"执行失败（退出码 {e.returncode}）：\n{e.output or '无输出'}"
    except Exception as e:
        return f"错误：{str(e)}"