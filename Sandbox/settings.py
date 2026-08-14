from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "nmap",
        "curl",
        "subfinder",
        "amass",
        "httpx",
        "ffuf",
        "dirsearch",
        "nuclei",
        "pwsh",
    }
)
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


def _configured_tools() -> frozenset[str]:
    raw = os.environ.get("QINYUAN_SANDBOX_ALLOWED_TOOLS")
    if raw is None:
        return DEFAULT_ALLOWED_TOOLS
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError("QINYUAN_SANDBOX_ALLOWED_TOOLS must not be empty")
    return values


def _configured_boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class SandboxSettings:
    image: str = "qinyuan/pentest-sandbox:0.3.1"
    network: str = "qinyuan_pentest_lab"
    network_internal: bool = True

    memory_limit: str = "512m"
    memory_swap_limit: str = "512m"
    cpu_limit: int = 500_000_000
    process_limit: int = 128

    workspace_size: str = "128m"
    temporary_size: str = "64m"

    maximum_timeout_seconds: int = 300

    # Docker 初始化连接应快速失败，避免 Docker Desktop 异常时等待很久。
    docker_connect_timeout_seconds: int = 10

    # 命令最长可运行 300 秒，因此后续 Docker API 读取超时要更长。
    docker_api_timeout_seconds: int = 315

    # 每个 stdout/stderr 最多在宿主机内存中保留 128 KiB。
    maximum_output_bytes_per_stream: int = 128 * 1024

    allowed_tools: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_TOOLS)

    @classmethod
    def from_environment(cls) -> "SandboxSettings":
        return cls(
            image=os.environ.get(
                "QINYUAN_SANDBOX_IMAGE",
                "qinyuan/pentest-sandbox:0.3.1",
            ).strip(),
            network=os.environ.get(
                "QINYUAN_SANDBOX_NETWORK",
                "qinyuan_pentest_lab",
            ).strip(),
            network_internal=_configured_boolean(
                "QINYUAN_SANDBOX_NETWORK_INTERNAL",
                True,
            ),
            allowed_tools=_configured_tools(),
        )

    def __post_init__(self) -> None:
        """在程序启动阶段发现错误配置。"""

        if not self.image:
            raise ValueError("sandbox image must not be empty")

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.network):
            raise ValueError("sandbox network name is invalid")

        if not isinstance(self.network_internal, bool):
            raise ValueError("network_internal must be a boolean")

        if not self.allowed_tools or any(
            _TOOL_NAME_PATTERN.fullmatch(tool) is None
            for tool in self.allowed_tools
        ):
            raise ValueError("allowed sandbox tools must be command names")

        if self.maximum_timeout_seconds < 1:
            raise ValueError("maximum_timeout_seconds must be positive")

        if self.docker_connect_timeout_seconds < 1:
            raise ValueError("docker_connect_timeout_seconds must be positive")

        if self.docker_api_timeout_seconds <= self.maximum_timeout_seconds:
            raise ValueError(
                "docker_api_timeout_seconds must be greater than "
                "maximum_timeout_seconds"
            )

        if self.maximum_output_bytes_per_stream < 1024:
            raise ValueError(
                "maximum_output_bytes_per_stream must be at least 1024"
            )
