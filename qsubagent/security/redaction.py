from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEYS = {
    "authorization",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "cookie",
    "setcookie",
}
_PATTERNS = (
    re.compile(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;]+)"
    ),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|cookie)"
        r"\s*[:=]\s*)([^\s,;]+)"
    ),
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def redact_data(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _normalize_key(key) in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: redact_data(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
