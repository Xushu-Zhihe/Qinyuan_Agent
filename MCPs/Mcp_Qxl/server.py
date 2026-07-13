import shutil
import os

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nmap-mcp-server",json_response=True)

@mcp.tool(
    title="Mcp_Qxl",
    description=(
        
    ),
)