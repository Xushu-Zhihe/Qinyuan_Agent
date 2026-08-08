from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from .agent import AgentRunner
from .approval import ApprovalManager
from .config import AppSettings
from .provider_models import ProviderProcessResult
from .result import extract_protocol_line, failure_line
from .run_context import load_run_context
from .runtime import ActiveTurn, ThreadRepository
from .sandbox.auth import read_bearer_token
from .sandbox.client import SandboxBridgeClient
from .sandbox.contracts import BridgeProtocolError, RunContext
from .security import redact_text
from .mcp.manager import McpError, NmapMcpManager
from tools.profiles import PROFILE_VERSION, build_profile_registry, preflight_profile
from skills.loader import configure_limits


PROVIDER_VERSION = "1.0.0"


def _settings_for_run(settings: AppSettings, context: RunContext) -> AppSettings:
    work_dir = Path(context.work_dir).resolve()
    state_dir = work_dir / "state"
    artifact_root = Path(context.artifact_scope).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    return replace(
        settings,
        runtime=replace(
            settings.runtime,
            thread_id=context.thread_id,
            state_dir=state_dir,
        ),
        artifacts=replace(settings.artifacts, root=artifact_root),
        provider=replace(settings.provider, profile=context.tool_profile),
    )


def _profile_tools(context: RunContext, settings: AppSettings) -> tuple[Any, Any, Any]:
    bridge = None
    manager = None
    if context.tool_profile == "nmap_optional":
        token_path_value = os.environ.get("QINYUAN_SANDBOX_TOKEN_FILE", "")
        if not token_path_value:
            raise BridgeProtocolError("nmap_optional requires QINYUAN_SANDBOX_TOKEN_FILE")
        token_path = Path(token_path_value).resolve()
        work_dir = Path(context.work_dir).resolve()
        try:
            token_path.relative_to(work_dir)
        except ValueError as exc:
            raise BridgeProtocolError("sandbox token file is outside work_dir") from exc
        if token_path == work_dir / "run_context.json":
            raise BridgeProtocolError("sandbox token file cannot be run_context.json")
        token = read_bearer_token(token_path)
        bridge = SandboxBridgeClient(
            context.sandbox_endpoint,
            token,
            connect_timeout_seconds=settings.bridge.connect_timeout_seconds,
            response_grace_seconds=settings.bridge.response_grace_seconds,
            max_response_bytes=settings.bridge.max_response_bytes,
        )
        manager = NmapMcpManager(
            context,
            startup_timeout_seconds=settings.mcp.startup_timeout_seconds,
            call_timeout_seconds=settings.mcp.call_timeout_seconds,
            shutdown_timeout_seconds=settings.mcp.shutdown_timeout_seconds,
        )
    registry = build_profile_registry(
        context.tool_profile,
        context=context,
        bridge=bridge,
        manager=manager,
        settings=settings,
    )
    return registry, manager, bridge


def _status_from_state(repository: ThreadRepository) -> str:
    state = repository.load_state()
    durable_statuses = repository.tool_call_statuses()
    if "outcome_unknown" in durable_statuses:
        return "outcome_unknown"
    if "timeout" in durable_statuses:
        return "timeout"
    if any(
        call.get("status") in {"running", "outcome_unknown"}
        for call in state.pending_tool_calls.values()
    ):
        return "outcome_unknown"
    if state.status == "aborted":
        return "aborted"
    if state.paused_reason in {"model_timeout", "turn_timeout"}:
        return "timeout"
    return "failed"


async def run_provider_async(
    task_text: str,
    context: RunContext,
    *,
    settings: AppSettings | None = None,
    client: Any | None = None,
) -> ProviderProcessResult:
    if settings is None:
        from .config import DEFAULT_CONFIG_PATH

        config_path = Path(os.environ.get("QINYUAN_CONFIG_FILE", str(DEFAULT_CONFIG_PATH)))
        settings = AppSettings.load(config_path)
    settings = _settings_for_run(settings, context)
    configure_limits(
        content_max_bytes=settings.skills.content_max_bytes,
        resource_max_bytes=settings.skills.resource_max_bytes,
    )
    registry, manager, _bridge = _profile_tools(context, settings)
    repository = ThreadRepository(
        settings.runtime.state_dir,
        settings.runtime.thread_id,
        event_log_max_bytes=settings.runtime.event_log_max_bytes,
        event_log_backups=settings.runtime.event_log_backups,
        lease_ttl_seconds=settings.runtime.lease_ttl_seconds,
    )
    fake_response = os.environ.get("QINYUAN_FAKE_MODEL_RESPONSE")
    owned_client = client is None and fake_response is None
    if client is None:
        if fake_response is not None:
            class FakeResponses:
                async def create(self, **_: object) -> object:
                    return SimpleNamespace(
                        id="fake-response",
                        output=[
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": fake_response,
                            }
                        ],
                        output_text=fake_response,
                        usage=SimpleNamespace(input_tokens=0),
                    )

            client = SimpleNamespace(responses=FakeResponses())
        else:
            from openai import AsyncOpenAI

            client_kwargs: dict[str, object] = {
                "api_key": settings.model.api_key or None,
                "timeout": settings.runtime.model_timeout_seconds,
                "max_retries": 0,
            }
            if settings.model.base_url:
                client_kwargs["base_url"] = settings.model.base_url
            client = AsyncOpenAI(**client_kwargs)
    agent: AgentRunner | None = None
    turn_task: asyncio.Task[Any] | None = None
    loop = asyncio.get_running_loop()
    try:
        agent = AgentRunner(
            client=client,
            settings=settings,
            registry=registry,
            repository=repository,
            emit=lambda message: print(redact_text(str(message)), file=sys.stderr, flush=True),
            approval_manager=ApprovalManager(
                emit=lambda message: print(redact_text(str(message)), file=sys.stderr, flush=True),
                channel_available=False,
            ),
        )
        await agent.activate()
        turn = ActiveTurn()
        turn_task = asyncio.create_task(agent.continue_turn(turn, task_text))
        turn.task = turn_task
        agent.active_turn = turn

        def request_cancel() -> None:
            if turn_task is not None and not turn_task.done():
                asyncio.create_task(agent.interrupt())

        signal_installed = False
        if hasattr(loop, "add_signal_handler"):
            try:
                loop.add_signal_handler(signal.SIGTERM, request_cancel)
                signal_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            response = await turn_task
        except asyncio.CancelledError:
            return ProviderProcessResult("aborted", 130, error="provider cancelled")
        finally:
            if signal_installed:
                with __import__("contextlib").suppress(Exception):
                    loop.remove_signal_handler(signal.SIGTERM)

        if response is None:
            status = _status_from_state(repository)
            code = {"timeout": 124, "aborted": 130, "outcome_unknown": 70}.get(status, 1)
            return ProviderProcessResult(status, code, error=status)
        tool_status = _status_from_state(repository)
        if tool_status in {"timeout", "outcome_unknown"}:
            return ProviderProcessResult(
                tool_status,
                124 if tool_status == "timeout" else 70,
                error=f"tool outcome was {tool_status}",
            )
        protocol = extract_protocol_line(str(getattr(response, "output_text", "")))
        if protocol is None:
            return ProviderProcessResult(
                "failed",
                2,
                error="model did not return exactly one valid protocol line",
            )
        return ProviderProcessResult("completed", 0, protocol_line=protocol.line)
    except asyncio.CancelledError:
        return ProviderProcessResult("aborted", 130, error="provider cancelled")
    except Exception as exc:
        state_status = _status_from_state(repository) if agent is not None else "failed"
        status = "outcome_unknown" if state_status == "outcome_unknown" else "failed"
        code = 70 if status == "outcome_unknown" else 1
        return ProviderProcessResult(status, code, error=redact_text(type(exc).__name__ + ": " + str(exc)))
    finally:
        if agent is not None:
            await agent.close()
        elif repository.lease.acquired:
            repository.release_lease()
        if manager is not None:
            await manager.close()
        if owned_client and client is not None:
            await client.close()


async def _read_stdin() -> str:
    data = await asyncio.to_thread(sys.stdin.buffer.read)
    if not isinstance(data, bytes):
        raise ValueError("stdin did not return bytes")
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("task input is too large")
    return data.decode("utf-8")


async def provider_main_async(context_path: Path) -> int:
    try:
        context = load_run_context(context_path)
        task_text = await _read_stdin()
        if not task_text:
            raise BridgeProtocolError("stdin task is empty")
        result = await run_provider_async(task_text, context)
    except Exception as exc:
        result = ProviderProcessResult("failed", 1, error=redact_text(type(exc).__name__ + ": " + str(exc)))
    if result.protocol_line is not None:
        print(result.protocol_line, flush=True)
    else:
        print(
            failure_line(
                {
                    "status": result.status,
                    "error": result.error or result.status,
                }
            ),
            flush=True,
        )
    return result.exit_code


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qinyuan host provider")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--preflight", metavar="PROFILE")
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help="controlled run_context.json path (or QINYUAN_RUN_CONTEXT_FILE)",
    )
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(f"qinyuan-main {PROVIDER_VERSION} protocol=1 profile-version={PROFILE_VERSION}")
        return 0
    if arguments.preflight is not None:
        try:
            print(json.dumps(preflight_profile(arguments.preflight).to_dict(), separators=(",", ":")))
            return 0
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__}, separators=(",", ":")), file=sys.stderr)
            return 2
    context = arguments.context or (
        Path(os.environ["QINYUAN_RUN_CONTEXT_FILE"])
        if os.environ.get("QINYUAN_RUN_CONTEXT_FILE")
        else None
    )
    if context is None:
        parser.error("--context or QINYUAN_RUN_CONTEXT_FILE is required")
    return asyncio.run(provider_main_async(context))


if __name__ == "__main__":
    raise SystemExit(run())
