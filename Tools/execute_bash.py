from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from Sandbox.models import CommandRequest
from Sandbox.service import SandboxService


class SandboxCommandInput(BaseModel):
    tool: str = Field(description="Allowed executable name")
    arguments: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


def create_sandbox_command_tool(
    agent_id: str,
    sandbox_service: SandboxService,
) -> StructuredTool:
    """Create a tool permanently bound to one Agent sandbox."""

    def execute(
        tool: str,
        arguments: list[str],
        timeout_seconds: int = 30,
    ) -> dict:
        result = sandbox_service.execute(
            agent_id=agent_id,
            request=CommandRequest(
                tool=tool,
                arguments=tuple(arguments),
                timeout_seconds=timeout_seconds,
            ),
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }

    return StructuredTool.from_function(
        func=execute,
        name="execute_sandbox_command",
        description="Execute an allow-listed command in this Agent's sandbox.",
        args_schema=SandboxCommandInput,
    )