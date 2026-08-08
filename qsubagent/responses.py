from __future__ import annotations

import json
from typing import Any


def extract_tool_calls(response: Any) -> dict[str, dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for item in response.output:
        if item_field(item, "type") != "function_call":
            continue

        call_id = str(item_field(item, "call_id"))
        arguments = item_field(item, "arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)

        pending[call_id] = {
            "call_id": call_id,
            "name": str(item_field(item, "name")),
            "arguments": arguments,
            "status": "pending",
            "output": None,
        }
    return pending


def build_tool_outputs(
    pending_calls: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for call_id, call in pending_calls.items():
        status = str(call.get("status", "aborted"))
        if status in {"completed", "failed", "timeout"} and call.get("output") is not None:
            output = str(call.get("output", ""))
        else:
            error = (
                "tool_outcome_unknown"
                if status in {"running", "outcome_unknown"}
                else "tool_aborted"
            )
            output = json.dumps(
                {
                    "ok": False,
                    "error": error,
                    "message": (
                        "Tool execution may have partially executed; "
                        "do not retry automatically"
                        if error == "tool_outcome_unknown"
                        else "Tool execution was interrupted before completion"
                    ),
                },
                ensure_ascii=False,
            )
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            }
        )
    return outputs


def parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    parsed = json.loads(str(arguments))
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return parsed


def item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def get_input_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get("input_tokens")
    else:
        value = getattr(usage, "input_tokens", None)
    return int(value) if value is not None else None
