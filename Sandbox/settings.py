from __future__ import annotations

from dataclasses import dataclass,field

@dataclass(frozen=True)
class SandboxSettings:
    image: str = "qinyuan/pentest-sandbox:0.2.0"
    network: str = "qinyuan_pentest_lab"
    
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

    allowed_tools: frozenset[str] = field(
        default_factory = lambda: frozenset(
            {
                "nmap",
                "curl",
                "subfinder",
                "amass",
                "httpx",
                "ffuf",
                "dirsearch",
                "nuclei",
            }
        )
    )

    def __post_init__(self) -> None:
        """在程序启动阶段发现错误配置。"""

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
