from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass(frozen=True)
class ModelSettings:
    name: str
    api_key: str
    base_url: str
    api_key_env: str = "OPENAI_API_KEY"


@dataclass(frozen=True)
class CapabilitySettings:
    previous_response_id: bool = True
    prompt_cache_key: bool = False
    server_compaction: bool = False
    local_compaction: bool = True
    streaming: bool = False
    store: bool = True


@dataclass(frozen=True)
class RuntimeSettings:
    thread_id: str = "default"
    state_dir: Path = PROJECT_ROOT / "var" / "threads"
    interrupt_grace_seconds: float = 0.5
    soft_tool_rounds: int = 16
    hard_tool_rounds: int = 64
    repeated_call_limit: int = 3
    turn_timeout_seconds: float = 600.0
    model_timeout_seconds: float = 90.0
    tool_timeout_seconds: float = 120.0
    model_max_retries: int = 0
    retry_initial_seconds: float = 0.5
    context_window: int = 128_000
    compact_ratio: float = 0.9
    compaction_max_output_tokens: int = 2_048
    lease_ttl_seconds: float = 30.0
    event_log_max_bytes: int = 10 * 1024 * 1024
    event_log_backups: int = 3

    @property
    def compact_threshold(self) -> int:
        return int(self.context_window * self.compact_ratio)


@dataclass(frozen=True)
class ArtifactSettings:
    root: Path = PROJECT_ROOT / "var" / "artifacts"
    memory_limit_bytes: int = 128 * 1024
    preview_bytes: int = 16 * 1024
    file_limit_bytes: int = 128 * 1024 * 1024
    thread_limit_bytes: int = 1024 * 1024 * 1024
    retention_days: int = 30
    read_max_bytes: int = 256 * 1024


@dataclass(frozen=True)
class PowerShellSettings:
    mode: str = "unrestricted"


@dataclass(frozen=True)
class SkillSettings:
    content_max_bytes: int = 256 * 1024
    resource_max_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class AppSettings:
    model: ModelSettings
    capabilities: CapabilitySettings
    runtime: RuntimeSettings
    artifacts: ArtifactSettings = field(default_factory=ArtifactSettings)
    powershell: PowerShellSettings = field(default_factory=PowerShellSettings)
    skills: SkillSettings = field(default_factory=SkillSettings)

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_CONFIG_PATH,
        *,
        execution_mode: str | None = None,
    ) -> "AppSettings":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("config.yaml 顶层必须是映射")
        model_raw = _mapping(raw, "model")
        capabilities_raw = _mapping(raw, "capabilities")
        runtime_raw = _mapping(raw, "runtime")
        artifacts_raw = _mapping(raw, "artifacts")
        powershell_raw = _mapping(raw, "powershell")
        skills_raw = _mapping(raw, "skills")

        state_dir = _path(runtime_raw.get("state_dir", "var/threads"))
        artifact_root = _path(artifacts_raw.get("root", "var/artifacts"))
        api_key_env = str(model_raw.get("api_key_env", "OPENAI_API_KEY")).strip()
        if not api_key_env:
            raise ValueError("model.api_key_env 不能为空")

        mode = execution_mode or str(
            powershell_raw.get("mode", "unrestricted")
        ).strip()
        settings = cls(
            model=ModelSettings(
                name=str(model_raw.get("name", "")).strip(),
                api_key=os.environ.get(api_key_env, "").strip(),
                base_url=str(model_raw.get("base_url", "")).strip(),
                api_key_env=api_key_env,
            ),
            capabilities=CapabilitySettings(
                previous_response_id=_strict_bool(
                    capabilities_raw,
                    "previous_response_id",
                    True,
                ),
                prompt_cache_key=_strict_bool(
                    capabilities_raw,
                    "prompt_cache_key",
                    False,
                ),
                server_compaction=_strict_bool(
                    capabilities_raw,
                    "server_compaction",
                    False,
                ),
                local_compaction=_strict_bool(
                    capabilities_raw,
                    "local_compaction",
                    True,
                ),
                streaming=_strict_bool(capabilities_raw, "streaming", False),
                store=_strict_bool(capabilities_raw, "store", True),
            ),
            runtime=RuntimeSettings(
                thread_id=str(runtime_raw.get("thread_id", "default")),
                state_dir=state_dir,
                interrupt_grace_seconds=_float(
                    runtime_raw, "interrupt_grace_seconds", 0.5
                ),
                soft_tool_rounds=_int(runtime_raw, "soft_tool_rounds", 16),
                hard_tool_rounds=_int(runtime_raw, "hard_tool_rounds", 64),
                repeated_call_limit=_int(
                    runtime_raw, "repeated_call_limit", 3
                ),
                turn_timeout_seconds=_float(
                    runtime_raw, "turn_timeout_seconds", 600.0
                ),
                model_timeout_seconds=_float(
                    runtime_raw, "model_timeout_seconds", 90.0
                ),
                tool_timeout_seconds=_float(
                    runtime_raw, "tool_timeout_seconds", 120.0
                ),
                model_max_retries=_int(
                    runtime_raw, "model_max_retries", 0
                ),
                retry_initial_seconds=_float(
                    runtime_raw, "retry_initial_seconds", 0.5
                ),
                context_window=_int(runtime_raw, "context_window", 128_000),
                compact_ratio=_float(runtime_raw, "compact_ratio", 0.9),
                compaction_max_output_tokens=_int(
                    runtime_raw, "compaction_max_output_tokens", 2_048
                ),
                lease_ttl_seconds=_float(
                    runtime_raw, "lease_ttl_seconds", 30.0
                ),
                event_log_max_bytes=_int(
                    runtime_raw, "event_log_max_bytes", 10 * 1024 * 1024
                ),
                event_log_backups=_int(runtime_raw, "event_log_backups", 3),
            ),
            artifacts=ArtifactSettings(
                root=artifact_root,
                memory_limit_bytes=_int(
                    artifacts_raw, "memory_limit_bytes", 128 * 1024
                ),
                preview_bytes=_int(
                    artifacts_raw, "preview_bytes", 16 * 1024
                ),
                file_limit_bytes=_int(
                    artifacts_raw, "file_limit_bytes", 128 * 1024 * 1024
                ),
                thread_limit_bytes=_int(
                    artifacts_raw,
                    "thread_limit_bytes",
                    1024 * 1024 * 1024,
                ),
                retention_days=_int(artifacts_raw, "retention_days", 30),
                read_max_bytes=_int(
                    artifacts_raw, "read_max_bytes", 256 * 1024
                ),
            ),
            powershell=PowerShellSettings(mode=mode),
            skills=SkillSettings(
                content_max_bytes=_int(
                    skills_raw, "content_max_bytes", 256 * 1024
                ),
                resource_max_bytes=_int(
                    skills_raw, "resource_max_bytes", 2 * 1024 * 1024
                ),
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.model.name:
            raise ValueError("config.yaml 中的 model.name 不能为空")
        if self.powershell.mode not in {"unrestricted", "approval"}:
            raise ValueError(
                "powershell.mode 必须是 unrestricted 或 approval"
            )
        if not 0 < self.runtime.compact_ratio < 1:
            raise ValueError("runtime.compact_ratio 必须在 0 和 1 之间")
        positive_values = {
            "runtime.soft_tool_rounds": self.runtime.soft_tool_rounds,
            "runtime.hard_tool_rounds": self.runtime.hard_tool_rounds,
            "runtime.repeated_call_limit": self.runtime.repeated_call_limit,
            "runtime.turn_timeout_seconds": self.runtime.turn_timeout_seconds,
            "runtime.model_timeout_seconds": self.runtime.model_timeout_seconds,
            "runtime.tool_timeout_seconds": self.runtime.tool_timeout_seconds,
            "runtime.context_window": self.runtime.context_window,
            "runtime.compaction_max_output_tokens": (
                self.runtime.compaction_max_output_tokens
            ),
            "runtime.lease_ttl_seconds": self.runtime.lease_ttl_seconds,
            "runtime.event_log_max_bytes": self.runtime.event_log_max_bytes,
            "runtime.event_log_backups": self.runtime.event_log_backups,
            "artifacts.memory_limit_bytes": self.artifacts.memory_limit_bytes,
            "artifacts.preview_bytes": self.artifacts.preview_bytes,
            "artifacts.file_limit_bytes": self.artifacts.file_limit_bytes,
            "artifacts.thread_limit_bytes": self.artifacts.thread_limit_bytes,
            "artifacts.retention_days": self.artifacts.retention_days,
            "artifacts.read_max_bytes": self.artifacts.read_max_bytes,
            "skills.content_max_bytes": self.skills.content_max_bytes,
            "skills.resource_max_bytes": self.skills.resource_max_bytes,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.runtime.hard_tool_rounds < self.runtime.soft_tool_rounds:
            raise ValueError("runtime.hard_tool_rounds 不能小于 soft_tool_rounds")
        if self.runtime.interrupt_grace_seconds < 0:
            raise ValueError("runtime.interrupt_grace_seconds 不能小于 0")
        if self.runtime.model_max_retries < 0:
            raise ValueError("runtime.model_max_retries 不能小于 0")
        if self.runtime.retry_initial_seconds < 0:
            raise ValueError("runtime.retry_initial_seconds 不能小于 0")
        if self.artifacts.preview_bytes * 2 > self.artifacts.memory_limit_bytes:
            raise ValueError(
                "artifacts.memory_limit_bytes 必须至少是 preview_bytes 的两倍"
            )
        if self.artifacts.file_limit_bytes > self.artifacts.thread_limit_bytes:
            raise ValueError(
                "artifacts.file_limit_bytes 不能大于 thread_limit_bytes"
            )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml 中的 {key} 必须是映射")
    return value


def _strict_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 YAML 布尔值 true 或 false")
    return value


def _int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return value


def _float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 必须是数字")
    return float(value)


def _path(value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else PROJECT_ROOT / path
