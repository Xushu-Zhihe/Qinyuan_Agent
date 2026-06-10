from mcp.server.fastmcp import FastMCP # type: ignore

from nuclei_mcp.tools import register_tools # type: ignore

mcp = FastMCP("nuclei-mcp-server", json_response=True)

register_tools(mcp)

if __name__ == "__main__":
    mcp.run()