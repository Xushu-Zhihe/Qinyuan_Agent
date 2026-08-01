from dataclasses import dataclass,field

@dataclass(frozen=True)
class SandboxSettings:
    image: str = "qinyuan/pentest-sandbox:dev"
    network: str = "qinyuan_pentest_lab"
    
    memory_limit: str = "512m"
    memory_swap_limit: str = "512m"
    cpu_limit: int = 500_000_000
    process_limit: int = 128

    workspace_size: str = "128m"
    temporary_size: str = "64m"

    maximum_timeout_seconds: int = 300

    allowed_tools: frozenset[str] = field(
        default_factory = lambda: frozenset(
            {
                "nmap",
                "curl",
            }
        )
    )