from __future__ import annotations

import asyncio
from pathlib import Path

from .client import build_mcp_runtime


async def main() -> None:
    manager = None

    try:
        registry_path = Path(__file__).with_name("registry.json")
        manager, adapter = await build_mcp_runtime(registry_path)

        print("MCP runtime started")

        tools = adapter.list_all_tool_specs()
        print(f"tools: {len(tools)}")
        for tool in tools:
            print(f"- {tool['qualified_name']}: {tool.get('description', '')}")

        print(f"resources: {len(manager.resource_index)}")
        for name in manager.resource_index:
            print(f"- {name}")

        print(f"prompts: {len(manager.prompt_index)}")
        for name in manager.prompt_index:
            print(f"- {name}")

    finally:
        if manager is not None:
            await manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())