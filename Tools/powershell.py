from __future__ import annotations

import json
import re
import shutil
from typing import TYPE_CHECKING

from qsubagent.config import ArtifactSettings, PROJECT_ROOT
from qsubagent.runtime import ArtifactStore, ToolResult
from qsubagent.runtime.process import run_streaming_process

from .registry import registry

if TYPE_CHECKING:
    from qsubagent.runtime import ActiveTurn


WORKSPACE_PATH = PROJECT_ROOT / "workspace"
POWERSHELL_EXECUTABLE = shutil.which("powershell.exe") or shutil.which(
    "pwsh.exe"
)


async def powershell(
    command: str,
    turn: "ActiveTurn | None" = None,
) -> ToolResult:
    if not POWERSHELL_EXECUTABLE:
        return ToolResult(
            output="Error: PowerShell executable not found",
            status="failed",
        )
    if turn is None:
        return ToolResult(
            output="Error: PowerShell requires an active turn",
            status="failed",
        )

    if turn.execution_mode == "approval":
        manager = turn.approval_manager
        if manager is None or not manager.channel_available:
            return _denied("approval_channel_unavailable")
        approved = await manager.request(command, turn.cancel_event)
        if turn.cancel_event.is_set():
            return ToolResult(
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "tool_aborted",
                        "message": "Approval wait was interrupted",
                    },
                    ensure_ascii=False,
                ),
                status="aborted",
            )
        if not approved:
            return _denied("denied_by_user")

    artifact_store = turn.artifact_store or ArtifactStore(ArtifactSettings())
    call_name = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        turn.running_call_id or "powershell",
    )
    utf8_command = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "$OutputEncoding = [Console]::OutputEncoding; "
        "$PSDefaultParameterValues['Get-Content:Encoding'] = 'UTF8'; "
        + command
    )
    return await run_streaming_process(
        POWERSHELL_EXECUTABLE,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8_command],
        cwd=WORKSPACE_PATH,
        turn=turn,
        artifact_store=artifact_store,
        stdout_name=f"{call_name}.stdout.log",
        stderr_name=f"{call_name}.stderr.log",
    )


def _denied(error: str) -> ToolResult:
    return ToolResult(
        output=json.dumps(
            {
                "ok": False,
                "error": error,
                "message": "PowerShell command was not executed",
            },
            ensure_ascii=False,
        ),
        status="failed",
    )


POWERSHELL_SCHEMA = {
    "type": "function",
    "name": "powershell",
    "description": (
        "Execute a complete PowerShell command in the configured workspace. "
        "Large stdout and stderr are archived; use read_artifact for ranges."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "PowerShell command to execute without rewriting.",
            }
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": True,
}

registry.register(
    "powershell",
    POWERSHELL_SCHEMA,
    powershell,
    pass_turn=True,
    cancellable=True,
    side_effect=True,
)
