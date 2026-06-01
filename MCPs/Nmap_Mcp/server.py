import shutil
import subprocess #subprocess 用来启动外部命令，即实际执行nmap
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nmap-mcp-server",json_response=True)

@mcp.tool(
    title="Nmap TCP Port Scan",
    description=(
        "Run an Nmap TCP connect scan against a single target. "
        "Use this tool during reconnaissance to check whether specific TCP ports are open. "
        "Input target should be a hostname or IP address. "
        "Input ports should be a comma-separated port list such as '22,80,443'. "
        "Returns the executed command, exit status, stdout, and stderr."
    ),
)
def run_nmap_tcp_scan(
    target:str,#target:str 扫描目标
    ports:str="80,443,8080",#扫描端口
    timeout_seconds: int=60,
) -> dict[str,Any]:#Any类型提示里用，表示字典里的值可以是多种类型
    nmap_path = shutil.which("nmap") #寻找nmap可执行位置
    if not nmap_path:#nmap命令的实际路径
        raise RuntimeError("nmap is not installed or present" 
" in PATH")
    command = [ #最终要执行的命令参数列表
        nmap_path,
        "-sT",
        "-Pn",
        "-n",
        "-p",
        ports,
        target,
    ]

    try:
        completed = subprocess.run( #执行后的返回结果对象
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok":False,
            "target":target,
            "timeout":True,
            "command":command,
        }
    return{
        "ok":completed.returncode==0,
        "target":target,
        "returncode":completed.returncode,
        "stdout":completed.stdout,
        "stderr":completed.stderr,
        "command":command,
    }

def main()->None:
        mcp.run()

if __name__=="__main__":
        main()