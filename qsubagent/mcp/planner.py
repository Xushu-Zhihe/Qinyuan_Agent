from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

from qsubagent.sandbox.contracts import PROTOCOL_VERSION, BridgeProtocolError

from .models import NmapScanPlan, SandboxCommand


_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


class NmapPolicyError(ValueError):
    pass


class NmapPlanner:
    """Validate Nmap intent and produce a fixed argv sandbox command."""

    def __init__(
        self,
        authorized_targets: Iterable[str],
        *,
        max_ports: int = 128,
        max_timeout_seconds: float = 900.0,
    ) -> None:
        self.authorized_targets = tuple(
            target.strip() for target in authorized_targets if isinstance(target, str)
        )
        self.max_ports = max_ports
        self.max_timeout_seconds = max_timeout_seconds
        if max_ports <= 0 or max_timeout_seconds <= 0:
            raise ValueError("Nmap planner limits must be positive")

    def plan(
        self,
        *,
        call_id: str,
        target: str,
        ports: object,
        timeout_seconds: object = 60,
    ) -> NmapScanPlan:
        target = self._validate_target(target)
        parsed_ports = self._parse_ports(ports)
        timeout = self._parse_timeout(timeout_seconds)
        if not self._is_authorized(target):
            raise NmapPolicyError("target is not authorized for this run")
        if not isinstance(call_id, str) or not call_id or "\x00" in call_id:
            raise NmapPolicyError("call_id must be a non-empty opaque string")
        command = SandboxCommand(
            protocol_version=PROTOCOL_VERSION,
            call_id=call_id,
            tool="nmap",
            arguments=(
                "-sT",
                "-Pn",
                "-n",
                "-p",
                ",".join(str(port) for port in parsed_ports),
                target,
            ),
            timeout_seconds=timeout,
        )
        return NmapScanPlan(command=command, target=target, ports=parsed_ports)

    @staticmethod
    def _validate_target(target: object) -> str:
        if not isinstance(target, str) or not target.strip() or "\x00" in target:
            raise NmapPolicyError("target must be a non-empty string")
        target = target.strip()
        if len(target) > 253 or any(character in target for character in " ;|&$`\n\r\t"):
            raise NmapPolicyError("target contains invalid characters")
        try:
            ipaddress.ip_address(target)
        except ValueError:
            try:
                ipaddress.ip_network(target, strict=False)
            except ValueError:
                if _HOSTNAME.fullmatch(target) is None:
                    raise NmapPolicyError("target must be an IP address, CIDR or hostname")
        return target

    def _is_authorized(self, target: str) -> bool:
        target_ip = None
        target_network = None
        try:
            target_ip = ipaddress.ip_address(target)
        except ValueError:
            try:
                target_network = ipaddress.ip_network(target, strict=False)
            except ValueError:
                pass
        for allowed in self.authorized_targets:
            if not allowed:
                continue
            if allowed.casefold() == target.casefold():
                return True
            if target_ip is not None:
                try:
                    if target_ip in ipaddress.ip_network(allowed, strict=False):
                        return True
                except ValueError:
                    continue
            if target_network is not None:
                try:
                    if target_network.subnet_of(ipaddress.ip_network(allowed, strict=False)):
                        return True
                except ValueError:
                    continue
        return False

    def _parse_ports(self, ports: object) -> tuple[int, ...]:
        values: list[int] = []
        raw_values: list[object]
        if isinstance(ports, str):
            raw_values = [part.strip() for part in ports.split(",")]
        elif isinstance(ports, (list, tuple)):
            raw_values = list(ports)
        else:
            raise NmapPolicyError("ports must be a list or comma-separated string")
        if not raw_values:
            raise NmapPolicyError("at least one port is required")
        for raw in raw_values:
            if isinstance(raw, bool):
                raise NmapPolicyError("ports must be integers")
            if isinstance(raw, int):
                candidate = raw
                if not 1 <= candidate <= 65535:
                    raise NmapPolicyError("port is outside 1..65535")
                values.append(candidate)
                continue
            if isinstance(raw, str) and re.fullmatch(r"[0-9]+(?:-[0-9]+)?", raw):
                start_end = raw.split("-", 1)
                start = int(start_end[0])
                end = int(start_end[-1])
                if not 1 <= start <= end <= 65535:
                    raise NmapPolicyError("port range is invalid")
                if end - start > self.max_ports:
                    raise NmapPolicyError("port range is too large")
                values.extend(range(start, end + 1))
                continue
            raise NmapPolicyError("ports must contain integers or ranges")
        deduplicated = tuple(dict.fromkeys(values))
        if len(deduplicated) > self.max_ports:
            raise NmapPolicyError("too many ports")
        return tuple(sorted(deduplicated))

    def _parse_timeout(self, timeout_seconds: object) -> float:
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise NmapPolicyError("timeout_seconds must be a number")
        timeout = float(timeout_seconds)
        if not 0 < timeout <= self.max_timeout_seconds:
            raise NmapPolicyError("timeout_seconds is outside the allowed range")
        return timeout
