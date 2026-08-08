from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from urllib.parse import urlsplit

from .contracts import BridgeProtocolError, CommandRequest, CommandResult
from .errors import (
    BridgeAuthenticationError,
    BridgeExpiredError,
    BridgePolicyError,
    BridgeResponseError,
    BridgeTransportError,
    BridgeUnavailableError,
)


MAX_RESPONSE_BYTES = 512 * 1024


class SandboxBridgeClient:
    """Minimal one-request HTTP client pinned to an IPv4 loopback endpoint."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        *,
        connect_timeout_seconds: float = 5.0,
        response_grace_seconds: float = 10.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise BridgeProtocolError(
                "sandbox_endpoint must be http://127.0.0.1:<port>"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise BridgeProtocolError("sandbox_endpoint port is invalid") from exc
        if port is None or not 1 <= port <= 65535:
            raise BridgeProtocolError("sandbox_endpoint must contain a valid port")
        if not bearer_token or any(character.isspace() for character in bearer_token):
            raise BridgeProtocolError("sandbox bearer token is invalid")
        if connect_timeout_seconds <= 0 or response_grace_seconds <= 0:
            raise ValueError("bridge timeouts must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self.endpoint = f"http://127.0.0.1:{port}"
        self.host = "127.0.0.1"
        self.port = port
        self._token = bearer_token
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.response_grace_seconds = float(response_grace_seconds)
        self.max_response_bytes = int(max_response_bytes)

    async def execute(self, request: CommandRequest) -> CommandResult:
        request = CommandRequest.from_dict(request.to_dict())
        timeout = request.timeout_seconds + self.response_grace_seconds
        status, payload = await self._post("/execute", request.to_dict(), timeout)
        if status != 200:
            self._raise_http_error(status, payload)
        try:
            result = CommandResult.from_dict(payload)
        except BridgeProtocolError as exc:
            raise BridgeResponseError("bridge returned an invalid command result") from exc
        if result.call_id != request.call_id:
            raise BridgeResponseError("bridge response call_id does not match request")
        return result

    async def cancel(self, call_id: str) -> bool:
        if not isinstance(call_id, str) or not call_id:
            raise BridgeProtocolError("call_id must be a non-empty string")
        status, payload = await self._post(
            "/cancel",
            {"call_id": call_id},
            self.connect_timeout_seconds + self.response_grace_seconds,
        )
        if status != 200:
            self._raise_http_error(status, payload)
        if not isinstance(payload, dict) or set(payload) != {"cancelled"}:
            raise BridgeResponseError("bridge returned an invalid cancel result")
        cancelled = payload["cancelled"]
        if not isinstance(cancelled, bool):
            raise BridgeResponseError("bridge cancel result must be boolean")
        return cancelled

    async def _post(
        self,
        path: str,
        payload: object,
        timeout_seconds: float,
    ) -> tuple[int, object]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > 128 * 1024:
            raise BridgeProtocolError("bridge request body is too large")
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            f"Authorization: Bearer {self._token}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer: asyncio.StreamWriter | None = None
        request_written = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout_seconds,
            )
            writer.write(request)
            await writer.drain()
            request_written = True
            return await asyncio.wait_for(
                self._read_response(reader),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError, EOFError) as exc:
            error = BridgeTransportError("sandbox bridge request failed")
            error.request_may_have_started = request_written
            raise error from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def _read_response(self, reader: asyncio.StreamReader) -> tuple[int, object]:
        status_line = await reader.readline()
        if not status_line or len(status_line) > 4096:
            raise EOFError("invalid HTTP status line")
        try:
            version, status_text, _ = status_line.decode("ascii").split(" ", 2)
            status = int(status_text)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BridgeResponseError("invalid HTTP status line") from exc
        if not version.startswith("HTTP/"):
            raise BridgeResponseError("invalid HTTP response version")
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n"}:
                break
            if not line or len(line) > 8192 or b":" not in line:
                raise BridgeResponseError("invalid HTTP response headers")
            key, value = line.decode("iso-8859-1").split(":", 1)
            headers[key.casefold().strip()] = value.strip()
        try:
            content_length = int(headers.get("content-length", "-1"))
        except ValueError as exc:
            raise BridgeResponseError("invalid bridge content length") from exc
        if content_length < 0 or content_length > self.max_response_bytes:
            raise BridgeResponseError("bridge response is too large")
        body = await reader.readexactly(content_length)
        try:
            return status, json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeResponseError("bridge response is not UTF-8 JSON") from exc

    @staticmethod
    def _raise_http_error(status: int, payload: object) -> None:
        category = "internal"
        detail = "sandbox bridge request failed"
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), str):
                category = payload["error"]
            if isinstance(payload.get("detail"), str):
                detail = payload["detail"][:300]
        if status in {401, 403} and category == "auth":
            raise BridgeAuthenticationError(detail)
        if status == 410 or category == "expired":
            raise BridgeExpiredError(detail)
        if status in {400, 405, 413} or category == "protocol":
            raise BridgeResponseError(detail)
        if status == 409 or category in {"policy", "cancelled"}:
            raise BridgePolicyError(detail)
        if status == 503 or category == "sandbox_unavailable":
            raise BridgeUnavailableError(detail)
        raise BridgeResponseError(detail)
