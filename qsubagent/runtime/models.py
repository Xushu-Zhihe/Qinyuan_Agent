from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ThreadState:
    thread_id: str
    last_completed_response_id: str | None = None
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "idle"
    replay_history_on_next_request: bool = False
    last_input_tokens: int | None = None
    active_turn_id: str | None = None
    paused_reason: str | None = None


@dataclass
class ActiveTurn:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    stream: Any | None = None
    process: asyncio.subprocess.Process | Any | None = None
    process_controller: Any | None = None
    tool_timeout_seconds: float = 120.0
    turn_id: str | None = None
    thread_id: str = "default"
    execution_mode: str = "unrestricted"
    approval_manager: Any | None = None
    artifact_store: Any | None = None
    running_call_id: str | None = None
    running_tool_side_effect: bool = False
    interrupted_tool_result: "ToolResult | None" = None


@dataclass(frozen=True)
class ToolResult:
    output: str
    status: str = "completed"
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
