from __future__ import annotations

import json
from pathlib import Path

from .sandbox.contracts import BridgeProtocolError, RunContext


def load_run_context(path: Path) -> RunContext:
    path = path.resolve()
    try:
        if path.stat().st_size > 128 * 1024:
            raise BridgeProtocolError("run context is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeProtocolError("run context is not valid UTF-8 JSON") from exc
    context = RunContext.from_dict(raw)
    work_dir = Path(context.work_dir).resolve()
    if path.parent != work_dir:
        raise BridgeProtocolError("run context file must be inside work_dir")
    if not work_dir.is_dir():
        raise BridgeProtocolError("run context work_dir does not exist")
    if context.is_expired():
        raise BridgeProtocolError("run context deadline has expired")
    return context
