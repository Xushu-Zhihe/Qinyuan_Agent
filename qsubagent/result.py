from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PROTOCOL_PREFIX = "BLACKBOARD_RESULT:"
INTENT_PREFIX = "BLACKBOARD_INTENT:"
ALLOWED_PREFIXES = (PROTOCOL_PREFIX, INTENT_PREFIX)


@dataclass(frozen=True, slots=True)
class ProtocolLine:
    prefix: str
    payload: dict[str, object]
    line: str


def extract_protocol_line(text: str) -> ProtocolLine | None:
    matches: list[ProtocolLine] = []
    for line in text.splitlines():
        prefix = next((item for item in ALLOWED_PREFIXES if line.startswith(item)), None)
        if prefix is None:
            continue
        raw = line[len(prefix) :].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            matches.append(ProtocolLine(prefix, payload, line))
    if len(matches) != 1:
        return None
    return matches[0]


def failure_line(result: dict[str, object]) -> str:
    return PROTOCOL_PREFIX + " " + json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    )
