from __future__ import annotations

from pathlib import Path
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

        self._sessions: dict[str,SandboxSession] = {}     
        self._agent_locks: dict[str, RLock] = {}

        self._registry_lock = RLock()
        self._closed = False

    def start_for_agent(self,agent_id: str) -> SandboxSession:
        agent_id = agent_id.strip()

        if not agent_id:
            raise SandboxError("agent_id connot be empty")

        with self._registry_lock:
            if self._closed:
                raise SandboxError("SandboxService is closed")

            existing_session = self._sessions.get(agent_id)

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
            self._agent_locks[agent_id] = RLock()

            return session

    def execute(
        self,
        agent_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        self._validate_command(request)

        with self._registry_lock:
            session = self._sessions.get(agent_id)
            agent_lock = self._agent_locks.get(agent_id)

        if session is None or agent_lock is None:
            raise SandboxNotFoundError(
                f"Agent {agent_id!r} does not have a sandbox"
            )

        with agent_lock:
            with self._registry_lock:
                current_session = self._sessions.get(agent_id)

            if(
                current_session is None
                or current_session.sandbox_id != session.sandbox_id
            ):
                raise SandboxNotFoundError(
                    f"Sandbox for agent {agent_id!r} was removed"
                )

            if current_session.status != SandboxStatus.RUNNING:
                raise SandboxError(
                    f"Sandbox {current_session.sandbox_id!r}"
                    "is not running"
                )
            
            return self.runtime.execute(
                sandbox_id=session.sandbox_id,
                request=request,
            )

    def stop_for_agent(self,agent_id: str) -> None:
        with self._registry_lock:
            agent_lock = self._agent_locks.get(agent_id)

        if agent_lock is None:
            return 

        with agent_lock:
            with self._registry_lock:
                session = self._sessions.pop(agent_id,None)
                self._agent_locks.pop(agent_id,None)

            if session is not None:
                self.runtime.destroy(session.sandbox_id)

    def get_session(self,agent_id: str) -> SandboxSession | None :
        with self._registry_lock:
            return self._sessions.get(agent_id)

    def stop_all(self) -> None:
        with self._registry_lock:
            agent_ids = tuple(self._sessions.keys())

        for agent_id in agent_ids:
            self.stop_for_agent(agent_id)

    def close(self) -> None:
        with self._registry_lock:
            if self._closed:
                return

            self._closed = True

        self.stop_all()
        self.runtime.close()

    def _validate_command(self,request: CommandRequest) -> None:

        tool_name = request.tool.strip()

        if not tool_name:
            raise SandboxPolicyError("Tool cannot be empty")

        if(
            Path(tool_name).name != tool_name
            or "/" in tool_name
            or "\\" in tool_name
        ):
            raise SandboxPolicyError(
                "Tool must be a command name, not a filesystem path"
            )

        if tool_name not in self.settings.allowed_tools:
            raise SandboxPolicyError(
                f"Tool {tool_name!r} is not allowed"
            )

        if not (
            1
            <=request.timeout_seconds
            <=self.settings.maximum_timeout_seconds
        ):
            raise SandboxPolicyError(
                "Command timeout is outside the allowed range"
            )

        if any(
            not isinstance(argument,str)
            for argument in request.arguments
        ):
            raise SandboxPolicyError(
                "Every command argument must be a string"
            )

        if any(
            "\x00" in argument
            for argument in request.arguments
        ):
            raise SandboxPolicyError(
                "Command arguments cannot contain null bytes"
            )