from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

class SandboxStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"

@dataclass(frozen=True,slots=True)
class SandboxSession:
    agent_id: str
    sandbox_id: str
    status: SandboxStatus

@dataclass(frozen=True,slots=True)
class CommandRequest:
    tool: str
    arguments: tuple[str,...] = ()
    timeout_seconds: int = 30

    @property
    def argv(self) -> list[str]:
        return [self.tool,*self.arguments]
     
@dataclass(frozen=True,slots=True)
class CommandResult:
    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str

    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
