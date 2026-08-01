from __future__ import annotations

from typing import Protocol

from Sandbox.models import CommandRequest, CommandResult

class SandboxRuntime(Protocol):
    def create(self,agent_id: str) -> str:
        """创建沙盒并返回 sandbox_id."""
        ...
        
    def execute(
        self,
        sandbox_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        """在指定沙盒中执行命令."""
        ...
    
    def destroy(self,sandbox_id: str) -> None:
        """销毁指定沙盒."""
        ...
    
    def close(self) -> None:
        """释放 Runtime 使用的资源."""
        ...
    