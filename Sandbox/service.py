from __future__ import annotations

from pathlib import path
from threading import RLock

from Sandbox.contracts import SandboxRuntime
from Sandbox.models import (
    CommandRequest,
    CommandResult,
    SandboxSession,
    SandboxStatus,
)
from Sandbox.settings import SandboxSettings

class SandboxError(RuntimeError):
    pass

class SandboxNotFoundError(SandboxError):
    pass

class SandboxPolicyError(SandboxError):
    pass

class SandboxService:
    """
    一个 Agent对应一个独立沙盒。

    Docker 的创建、执行和销毁由 SandboxRuntime实现。
    SandboxService只负责 Agent与沙盒的生命周期映射。
    """

    def __init__(
        self,
        runtime: SandboxRuntime,
        settings: SandboxSettings | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or SandboxSettings()

        self._sessions = dict[str,SandboxSession] = {}
        self._lock = RLock()

    def start_for_agent(self,agent_id: str) -> SandboxSession:
        agent_id = agent_id.strip()

        if not agent_id:
            raise SandboxError("agent_id connot be empty")

        with self._lock:
            existing_session = self._session.get(agent_id)

            if(
                existing_session is not None
                and existing_session.status == SandboxStatus.RUNNING
            ):
                return existing_session

            sandbox_id = self.runtime.create(agent_id)

            session = SandboxSession(
                agent_id=agent_id,
                sandbox_id=sandbox_id,
                status=SandboxStatus.RUNNING,
            )

            self._sessions[agent_id] = session
            return session

    def execute(
        self,
        agent_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        self._validate_command(request)

        with self._lock:
            session = self._sessions.get(agent_id)

        if session is None:
            raise SandboxNotFoundError(
                f"Agent {agent_id!r} does not have a sandbox"
            )

        return self.runtime.execute(
            sandbox_id=session.sandbox_id,
            request=request,
        )

    def stop_for_agent(self,agent_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(agent_id,None)

        if session is not None:
            self.runtime.destory(session.sandbox_id)

    def get_session(self,agent_id: str) -> SandboxSession | None :
        with self._lock:
            return self._sessions.get(agent_id)

    def stop_all(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            self.runtiem.destory(session.sandbox_id)

    def close(self) -> None:
        self.stop_all()
        self.runtime.close()

    def _validate_command(self,request: CommandResult) -> None:
        tool_name = Path(request.tool).name

        if tool_name != request.tool:
            raise SandboxPolicyError(
                "Tool must be a command name, not a filesystem path"
            )

        if tool_name not in self.settings.allowed_tools:
            raise SandboxPolicyError(
                f"Tool {tool_name!r} is not allowed"
            )

        if not(
            1
            <= request.timeout_seconds
            <= self.settings.maximun_timeout_seconds
        ):
            raise SandboxPolicyError(
                "Command timeout is outside the allowed range"
            )

        if any("\x00" in argument for argument in request.arguments):
            raise SandboxPolicyError(
                "Command arguments cannot contain null bytes"
            )