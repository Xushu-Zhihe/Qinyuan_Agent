from __future__ import annotations

from pathlib import Path

from .contracts import BridgeProtocolError


def read_bearer_token(path: Path) -> str:
    try:
        if not path.is_file():
            raise BridgeProtocolError("sandbox token file does not exist")
        if path.stat().st_size > 1024:
            raise BridgeProtocolError("sandbox token file is too large")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BridgeProtocolError("sandbox token file cannot be read") from exc
    if (
        not token
        or len(token) > 512
        or "\x00" in token
        or any(character.isspace() for character in token)
    ):
        raise BridgeProtocolError("sandbox token file is invalid")
    return token
