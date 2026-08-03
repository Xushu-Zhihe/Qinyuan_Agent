from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

from qsubagent.runtime.models import ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        *,
        pass_turn: bool = False,
        cancellable: bool = False,
        side_effect: bool = False,
        read_only: bool = False,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = {
            "schema": schema,
            "handler": handler,
            "pass_turn": pass_turn,
            "cancellable": cancellable,
            "side_effect": side_effect,
            "read_only": read_only,
        }

    def get_schemas(self) -> list[dict[str, Any]]:
        return [info["schema"] for info in self._tools.values()]

    def get_metadata(self, name: str) -> dict[str, bool]:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        info = self._tools[name]
        return {
            "cancellable": bool(info["cancellable"]),
            "side_effect": bool(info["side_effect"]),
            "read_only": bool(info["read_only"]),
        }

    async def execute(
        self,
        tool_call: dict[str, Any],
        *,
        turn: Any | None = None,
    ) -> Any:
        tool_name = tool_call["name"]
        arguments = tool_call["arguments"]
        if tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {tool_name}")

        info = self._tools[tool_name]
        handler = info["handler"]
        keyword_arguments = dict(arguments)
        if info["pass_turn"]:
            keyword_arguments["turn"] = turn

        if inspect.iscoroutinefunction(handler):
            result = await handler(**keyword_arguments)
        else:
            result = await asyncio.to_thread(handler, **keyword_arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult(output=str(result))


registry = ToolRegistry()
