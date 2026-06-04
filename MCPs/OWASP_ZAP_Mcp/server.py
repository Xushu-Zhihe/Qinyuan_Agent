import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP


DEFAULT_ZAP_API_BASE = "http://127.0.0.1:8080"
MAX_WAIT_SECONDS = 600
MAX_ALERTS = 500
POLL_INTERVAL_SECONDS = 2

mcp = FastMCP("owasp-zap-mcp-server", json_response=True)


def _validate_url(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty")
    if len(cleaned) > 2048:
        raise ValueError(f"{field_name} is too long")
    if any(char in cleaned for char in "\r\n\t"):
        raise ValueError(f"{field_name} contains unsupported whitespace")

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not include credentials")

    return cleaned.rstrip("/") if field_name == "api_base" else cleaned


def _validate_int_range(value: int, field_name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _zap_request(
    api_base: str,
    component: str,
    action_type: str,
    name: str,
    params: dict[str, Any] | None = None,
    api_key: str = "",
    request_timeout_seconds: int = 15,
) -> dict[str, Any]:
    query_params = dict(params or {})
    if api_key:
        query_params["apikey"] = api_key

    url = f"{api_base}/JSON/{component}/{action_type}/{name}/"
    if query_params:
        url = f"{url}?{urlencode(query_params)}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Qinyuan-OWASP-ZAP-MCP/0.1",
        },
    )

    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ZAP API returned HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to ZAP API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Timed out while connecting to ZAP API") from exc

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ZAP API returned non-JSON response: {raw_body[:200]}") from exc

    if isinstance(payload, dict) and "code" in payload and "message" in payload:
        raise RuntimeError(f"ZAP API error {payload['code']}: {payload['message']}")

    return payload


def _wait_for_scan(
    api_base: str,
    api_key: str,
    component: str,
    scan_id: str,
    deadline: float,
) -> dict[str, Any]:
    last_status = 0

    while True:
        status_payload = _zap_request(
            api_base,
            component,
            "view",
            "status",
            {"scanId": scan_id},
            api_key,
        )
        last_status = int(status_payload.get("status", 0))
        if last_status >= 100:
            return {"complete": True, "status": last_status}
        if time.monotonic() >= deadline:
            return {"complete": False, "status": last_status}
        time.sleep(POLL_INTERVAL_SECONDS)


def _wait_for_passive_scan(api_base: str, api_key: str, deadline: float) -> dict[str, Any]:
    records_to_scan = 0

    while True:
        records_payload = _zap_request(
            api_base,
            "pscan",
            "view",
            "recordsToScan",
            api_key=api_key,
        )
        records_to_scan = int(records_payload.get("recordsToScan", 0))
        if records_to_scan <= 0:
            return {"complete": True, "records_to_scan": records_to_scan}
        if time.monotonic() >= deadline:
            return {"complete": False, "records_to_scan": records_to_scan}
        time.sleep(POLL_INTERVAL_SECONDS)


def _fetch_alerts(
    api_base: str,
    api_key: str,
    target_url: str,
    max_alerts: int,
) -> list[dict[str, Any]]:
    payload = _zap_request(
        api_base,
        "core",
        "view",
        "alerts",
        {
            "baseurl": target_url,
            "start": 0,
            "count": max_alerts,
        },
        api_key,
    )
    alerts = payload.get("alerts", [])
    return [_normalize_alert(alert) for alert in alerts if isinstance(alert, dict)]


def _normalize_alert(alert: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "risk",
        "confidence",
        "name",
        "url",
        "method",
        "param",
        "evidence",
        "description",
        "solution",
        "reference",
        "cweid",
        "wascid",
        "pluginId",
    )
    return {
        key: alert.get(key)
        for key in fields
        if alert.get(key) not in (None, "")
    }


def _summarize_alerts(alerts: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "High": 0,
        "Medium": 0,
        "Low": 0,
        "Informational": 0,
        "Unknown": 0,
    }
    for alert in alerts:
        risk = str(alert.get("risk", "Unknown"))
        summary[risk if risk in summary else "Unknown"] += 1
    return summary


def _error_response(error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": type(error).__name__,
        "error": str(error),
    }


@mcp.tool(
    title="OWASP ZAP API Status",
    description=(
        "Check whether the local OWASP ZAP API is reachable. "
        "Use this before running scans. "
        "The default API base is http://127.0.0.1:8080."
    ),
)
def get_zap_api_status(
    api_base: str = DEFAULT_ZAP_API_BASE,
    api_key: str = "",
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    try:
        api_base = _validate_url(api_base, "api_base")
        timeout_seconds = _validate_int_range(timeout_seconds, "timeout_seconds", 1, 30)
        payload = _zap_request(
            api_base,
            "core",
            "view",
            "version",
            api_key=api_key,
            request_timeout_seconds=timeout_seconds,
        )
        return {
            "ok": True,
            "api_base": api_base,
            "version": payload.get("version"),
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool(
    title="OWASP ZAP Spider and Passive Scan",
    description=(
        "Use OWASP ZAP to visit a single authorized HTTP/HTTPS target, run a spider crawl, "
        "wait for passive scanning, and return structured alerts. "
        "ZAP must already be running with its API available."
    ),
)
def run_zap_spider_passive_scan(
    target_url: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    api_key: str = "",
    timeout_seconds: int = 120,
    max_alerts: int = 100,
) -> dict[str, Any]:
    try:
        target_url = _validate_url(target_url, "target_url")
        api_base = _validate_url(api_base, "api_base")
        timeout_seconds = _validate_int_range(
            timeout_seconds,
            "timeout_seconds",
            1,
            MAX_WAIT_SECONDS,
        )
        max_alerts = _validate_int_range(max_alerts, "max_alerts", 1, MAX_ALERTS)

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds

        _zap_request(
            api_base,
            "core",
            "action",
            "accessUrl",
            {
                "url": target_url,
                "followRedirects": "true",
            },
            api_key,
        )
        scan_payload = _zap_request(
            api_base,
            "spider",
            "action",
            "scan",
            {
                "url": target_url,
                "recurse": "true",
                "subtreeOnly": "true",
            },
            api_key,
        )
        scan_id = str(scan_payload.get("scan", ""))
        if not scan_id:
            raise RuntimeError(f"ZAP did not return a spider scan id: {scan_payload}")

        spider = _wait_for_scan(api_base, api_key, "spider", scan_id, deadline)
        if not spider["complete"]:
            return {
                "ok": False,
                "timeout": True,
                "scan_type": "spider_passive",
                "target_url": target_url,
                "scan_id": scan_id,
                "spider_status": spider["status"],
            }

        passive = _wait_for_passive_scan(api_base, api_key, deadline)
        alerts = _fetch_alerts(api_base, api_key, target_url, max_alerts)

        return {
            "ok": passive["complete"],
            "timeout": not passive["complete"],
            "scan_type": "spider_passive",
            "target_url": target_url,
            "scan_id": scan_id,
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "spider_status": spider["status"],
            "passive_records_to_scan": passive["records_to_scan"],
            "alerts_count": len(alerts),
            "alerts_by_risk": _summarize_alerts(alerts),
            "alerts": alerts,
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool(
    title="OWASP ZAP Active Scan",
    description=(
        "Run an OWASP ZAP active scan against a single authorized HTTP/HTTPS target "
        "and return structured alerts. Active scanning can send attack traffic, so only "
        "use it for systems you are allowed to test."
    ),
)
def run_zap_active_scan(
    target_url: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    api_key: str = "",
    timeout_seconds: int = 300,
    max_alerts: int = 100,
) -> dict[str, Any]:
    try:
        target_url = _validate_url(target_url, "target_url")
        api_base = _validate_url(api_base, "api_base")
        timeout_seconds = _validate_int_range(
            timeout_seconds,
            "timeout_seconds",
            1,
            MAX_WAIT_SECONDS,
        )
        max_alerts = _validate_int_range(max_alerts, "max_alerts", 1, MAX_ALERTS)

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds

        scan_payload = _zap_request(
            api_base,
            "ascan",
            "action",
            "scan",
            {
                "url": target_url,
                "recurse": "true",
                "inScopeOnly": "false",
            },
            api_key,
        )
        scan_id = str(scan_payload.get("scan", ""))
        if not scan_id:
            raise RuntimeError(f"ZAP did not return an active scan id: {scan_payload}")

        active = _wait_for_scan(api_base, api_key, "ascan", scan_id, deadline)
        alerts = _fetch_alerts(api_base, api_key, target_url, max_alerts)

        return {
            "ok": active["complete"],
            "timeout": not active["complete"],
            "scan_type": "active",
            "target_url": target_url,
            "scan_id": scan_id,
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "active_scan_status": active["status"],
            "alerts_count": len(alerts),
            "alerts_by_risk": _summarize_alerts(alerts),
            "alerts": alerts,
        }
    except Exception as exc:
        return _error_response(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
