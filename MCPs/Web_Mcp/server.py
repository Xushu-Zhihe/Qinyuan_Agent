from __future__ import annotations

import base64
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "qinyuan-web-command-planner",
    instructions=(
        "Prepare authorized HTTP requests for execution inside the current "
        "Agent sandbox. This server never sends requests from the host."
    ),
    json_response=True,
)

_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.getenv(
        "MCP_WEB_ALLOWED_HOSTS",
        "127.0.0.1,localhost,target-web",
    )
    rules = tuple(item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip())
    if not rules:
        raise RuntimeError("MCP_WEB_ALLOWED_HOSTS cannot be empty")
    return rules


def _validate_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > 2_048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError("url is invalid")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain credentials")
    host = parsed.hostname.lower().rstrip(".")
    if not any(
        host == rule or (rule.startswith("*.") and host.endswith(rule[1:]))
        for rule in _allowed_hosts()
    ):
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def _validate_method(value: str) -> str:
    method = value.strip().upper()
    if method not in _METHODS:
        raise ValueError(f"method must be one of {sorted(_METHODS)}")
    return method


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 120:
        raise ValueError("timeout_seconds must be an integer between 5 and 120")
    return value


def _validate_headers(value: dict[str, str] | None) -> dict[str, str]:
    headers = value or {}
    if not isinstance(headers, dict) or len(headers) > 20:
        raise ValueError("headers must contain at most 20 string entries")
    result: dict[str, str] = {}
    for name, header_value in headers.items():
        if not isinstance(name, str) or _HEADER_NAME.fullmatch(name) is None:
            raise ValueError(f"invalid header name: {name!r}")
        if (
            not isinstance(header_value, str)
            or len(header_value) > 2_048
            or any(c in header_value for c in "\r\n\0")
        ):
            raise ValueError(f"invalid value for header {name!r}")
        result[name] = header_value
    return result


def _validate_body(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64 * 1024:
        raise ValueError("body must be a string no larger than 64 KiB")
    if "\0" in value:
        raise ValueError("body must not contain null bytes")
    return value


@mcp.tool(title="Sandbox HTTP Request")
def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    follow_redirects: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Prepare a bounded Curl request inside the Agent sandbox."""

    target = _validate_url(url)
    normalized_method = _validate_method(method)
    normalized_headers = _validate_headers(headers)
    normalized_body = _validate_body(body)
    timeout = _validate_timeout(timeout_seconds)
    arguments = [
        "--silent",
        "--show-error",
        "--include",
        "--max-time",
        str(timeout),
        "--request",
        normalized_method,
    ]
    if follow_redirects:
        arguments.extend(["--location", "--max-redirs", "5"])
    for name, value in normalized_headers.items():
        arguments.extend(["--header", f"{name}: {value}"])
    if normalized_body:
        arguments.extend(["--data-raw", normalized_body])
    arguments.extend(["--url", target])
    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": arguments,
            "timeout_seconds": timeout + 5,
        },
        "request": {
            "operation": "http-request",
            "target": target,
            "method": normalized_method,
            "body_present": bool(normalized_body),
        },
    }


_POWERSHELL_HTTP_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$requestData = ConvertFrom-Json (
    [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('__REQUEST_DATA__')
    )
) -AsHashtable
$parameters = @{
    Uri = $requestData.url
    Method = $requestData.method
    TimeoutSec = [int]$requestData.timeout_seconds
    Headers = $requestData.headers
    UseBasicParsing = $true
    MaximumRedirection = 5
    SkipHttpErrorCheck = $true
}
if ($requestData.body -ne '') { $parameters.Body = $requestData.body }
$response = Invoke-WebRequest @parameters
Write-Output ("HTTP status: {0}" -f [int]$response.StatusCode)
foreach ($item in $response.Headers.GetEnumerator()) {
    Write-Output ("{0}: {1}" -f $item.Key, ($item.Value -join ', '))
}
Write-Output ''
Write-Output $response.Content
""".strip()


@mcp.tool(title="Sandbox PowerShell HTTP Request")
def powershell_http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Prepare Invoke-WebRequest inside the Agent sandbox, never on the host."""

    target = _validate_url(url)
    normalized_method = _validate_method(method)
    normalized_headers = _validate_headers(headers)
    normalized_body = _validate_body(body)
    timeout = _validate_timeout(timeout_seconds)
    request_data = base64.b64encode(
        json.dumps(
            {
                "url": target,
                "method": normalized_method,
                "timeout_seconds": timeout,
                "body": normalized_body,
                "headers": normalized_headers,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    encoded_script = base64.b64encode(
        _POWERSHELL_HTTP_SCRIPT.replace(
            "__REQUEST_DATA__",
            request_data,
        ).encode("utf-16-le")
    ).decode("ascii")
    return {
        "sandbox_command": {
            "tool": "pwsh",
            "arguments": [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            "timeout_seconds": timeout + 5,
        },
        "request": {
            "operation": "powershell-http-request",
            "target": target,
            "method": normalized_method,
            "body_present": bool(normalized_body),
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
