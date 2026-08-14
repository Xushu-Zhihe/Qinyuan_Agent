from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


PROTOCOL_VERSION = 1
TOOL_PROFILES = frozenset(
    {"interactive_unrestricted", "no_tools", "nmap_optional"}
)
ALLOWED_TOOLS = frozenset({"nmap"})
COMMAND_FIELDS = frozenset(
    {"protocol_version", "call_id", "tool", "arguments", "timeout_seconds"}
)
RESULT_FIELDS = frozenset(
    {
        "protocol_version",
        "call_id",
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "stdout_truncated",
        "stderr_truncated",
        "artifact_refs",
    }
)
CONTEXT_FIELDS = frozenset(
    {
        "protocol_version",
        "agent_run_id",
        "thread_id",
        "tool_profile",
        "authorized_targets",
        "deadline",
        "work_dir",
        "artifact_scope",
        "result_contract",
        "sandbox_endpoint",
    }
)
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class BridgeProtocolError(ValueError):
    """A public v1 payload failed before any MCP or bridge side effect."""


def _strict_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BridgeProtocolError(f"{label} must be an object")
    extra = set(value) - fields
    missing = fields - set(value)
    if extra:
        raise BridgeProtocolError(f"unexpected {label} fields: {sorted(extra)}")
    if missing:
        raise BridgeProtocolError(f"missing {label} fields: {sorted(missing)}")
    return value


def _string(value: object, label: str, *, opaque: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BridgeProtocolError(f"{label} must be a non-empty string without NUL")
    result = value.strip()
    if opaque and _OPAQUE_ID.fullmatch(result) is None:
        raise BridgeProtocolError(f"{label} has an invalid opaque identifier")
    return result


def _json_bytes(value: object, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BridgeProtocolError(f"{label} must be JSON serializable") from exc
    if len(encoded) > 64 * 1024:
        raise BridgeProtocolError(f"{label} is too large")


@dataclass(frozen=True, slots=True)
class CommandRequest:
    protocol_version: int
    call_id: str
    tool: str
    arguments: tuple[str, ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise BridgeProtocolError("unsupported protocol_version")
        _string(self.call_id, "call_id", opaque=True)
        if self.tool not in ALLOWED_TOOLS or "/" in self.tool or "\\" in self.tool:
            raise BridgeProtocolError("tool is not allowed")
        if not isinstance(self.arguments, tuple) or len(self.arguments) > 128:
            raise BridgeProtocolError("arguments must contain at most 128 strings")
        for index, argument in enumerate(self.arguments):
            if not isinstance(argument, str) or "\x00" in argument:
                raise BridgeProtocolError(
                    f"arguments[{index}] must be a string without NUL"
                )
            if len(argument.encode("utf-8")) > 16 * 1024:
                raise BridgeProtocolError(f"arguments[{index}] is too large")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 0 < float(self.timeout_seconds) <= 900
        ):
            raise BridgeProtocolError("timeout_seconds must be in (0, 900]")

    @classmethod
    def from_dict(cls, value: object) -> "CommandRequest":
        raw = _strict_object(value, COMMAND_FIELDS, "command")
        version = raw["protocol_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise BridgeProtocolError("protocol_version must be an integer")
        arguments = raw["arguments"]
        if not isinstance(arguments, list):
            raise BridgeProtocolError("arguments must be a list")
        if any(not isinstance(item, str) for item in arguments):
            raise BridgeProtocolError("arguments must contain only strings")
        timeout = raw["timeout_seconds"]
        return cls(
            version,
            _string(raw["call_id"], "call_id", opaque=True),
            _string(raw["tool"], "tool"),
            tuple(arguments),
            float(timeout)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            else timeout,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "call_id": self.call_id,
            "tool": self.tool,
            "arguments": list(self.arguments),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    protocol_version: int = PROTOCOL_VERSION
    call_id: str = ""
    status: str = "completed"
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise BridgeProtocolError("unsupported protocol_version")
        _string(self.call_id, "call_id", opaque=True)
        if self.status not in {
            "completed",
            "failed",
            "timeout",
            "cancelled",
            "aborted",
            "outcome_unknown",
        }:
            raise BridgeProtocolError("invalid command status")
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise BridgeProtocolError("exit_code must be an integer or null")
        for label, value in (("stdout", self.stdout), ("stderr", self.stderr)):
            if not isinstance(value, str) or "\x00" in value:
                raise BridgeProtocolError(f"{label} must be a string without NUL")
        flags = (self.timed_out, self.stdout_truncated, self.stderr_truncated)
        if not all(isinstance(item, bool) for item in flags):
            raise BridgeProtocolError("result flags must be booleans")
        if not isinstance(self.artifact_refs, tuple):
            raise BridgeProtocolError("artifact_refs must be string identifiers")
        for reference in self.artifact_refs:
            if (
                not isinstance(reference, str)
                or not reference
                or len(reference) > 512
                or "\x00" in reference
                or "/" in reference
                or "\\" in reference
                or ".." in reference
            ):
                raise BridgeProtocolError("artifact_refs must be opaque identifiers")

    @classmethod
    def from_dict(cls, value: object) -> "CommandResult":
        raw = _strict_object(value, RESULT_FIELDS, "result")
        refs = raw["artifact_refs"]
        if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
            raise BridgeProtocolError("artifact_refs must be a list of strings")
        version = raw["protocol_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise BridgeProtocolError("protocol_version must be an integer")
        if not all(isinstance(raw[key], str) for key in ("status", "stdout", "stderr")):
            raise BridgeProtocolError("status, stdout and stderr must be strings")
        if not all(
            isinstance(raw[key], bool)
            for key in ("timed_out", "stdout_truncated", "stderr_truncated")
        ):
            raise BridgeProtocolError("result flags must be booleans")
        return cls(
            version,
            _string(raw["call_id"], "call_id", opaque=True),
            raw["status"],
            raw["exit_code"],
            raw["stdout"],
            raw["stderr"],
            raw["timed_out"],
            raw["stdout_truncated"],
            raw["stderr_truncated"],
            tuple(refs),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"artifact_refs": list(self.artifact_refs)}


@dataclass(frozen=True, slots=True)
class RunContext:
    protocol_version: int
    agent_run_id: str
    thread_id: str
    tool_profile: str
    authorized_targets: tuple[str, ...]
    deadline: str
    work_dir: str
    artifact_scope: str
    result_contract: str | dict[str, object]
    sandbox_endpoint: str

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise BridgeProtocolError("unsupported protocol_version")
        _string(self.agent_run_id, "agent_run_id", opaque=True)
        _string(self.thread_id, "thread_id", opaque=True)
        if self.tool_profile not in TOOL_PROFILES:
            raise BridgeProtocolError("tool_profile must be no_tools or nmap_optional")
        if not isinstance(self.authorized_targets, tuple) or any(
            not isinstance(item, str) or not item.strip() or "\x00" in item
            for item in self.authorized_targets
        ):
            raise BridgeProtocolError("authorized_targets must be string tuple")
        _string(self.deadline, "deadline")
        try:
            parsed_deadline = datetime.fromisoformat(self.deadline.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BridgeProtocolError("deadline must be ISO-8601") from exc
        if parsed_deadline.tzinfo is None:
            raise BridgeProtocolError("deadline must include a timezone")
        for label, value in (
            ("work_dir", self.work_dir),
            ("artifact_scope", self.artifact_scope),
            ("sandbox_endpoint", self.sandbox_endpoint),
        ):
            _string(value, label)
        work_dir = Path(self.work_dir)
        artifact_scope = Path(self.artifact_scope)
        if not work_dir.is_absolute() or not artifact_scope.is_absolute():
            raise BridgeProtocolError("work_dir and artifact_scope must be absolute")
        try:
            artifact_scope.resolve().relative_to(work_dir.resolve())
        except ValueError as exc:
            raise BridgeProtocolError("artifact_scope must be inside work_dir") from exc
        if not isinstance(self.result_contract, (str, dict)):
            raise BridgeProtocolError("result_contract must be a string or object")
        _json_bytes(self.result_contract, "result_contract")
        _json_bytes(self.to_dict(), "run context")

    @classmethod
    def from_dict(cls, value: object) -> "RunContext":
        raw = _strict_object(value, CONTEXT_FIELDS, "run_context")
        targets = raw["authorized_targets"]
        if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
            raise BridgeProtocolError("authorized_targets must be a list of strings")
        version = raw["protocol_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise BridgeProtocolError("protocol_version must be an integer")
        result_contract = raw["result_contract"]
        if not isinstance(result_contract, (str, dict)):
            raise BridgeProtocolError("result_contract must be a string or object")
        return cls(
            version,
            _string(raw["agent_run_id"], "agent_run_id", opaque=True),
            _string(raw["thread_id"], "thread_id", opaque=True),
            _string(raw["tool_profile"], "tool_profile"),
            tuple(targets),
            _string(raw["deadline"], "deadline"),
            _string(raw["work_dir"], "work_dir"),
            _string(raw["artifact_scope"], "artifact_scope"),
            dict(result_contract) if isinstance(result_contract, dict) else result_contract,
            _string(raw["sandbox_endpoint"], "sandbox_endpoint"),
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(self.deadline.replace("Z", "+00:00"))
        return current >= deadline

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "authorized_targets": list(self.authorized_targets)}
