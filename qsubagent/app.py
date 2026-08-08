from __future__ import annotations

import argparse
import asyncio
from typing import Sequence

from .agent import AgentRunner
from .approval import ApprovalManager
from .config import AppSettings
from .console import run_console
from .runtime import ThreadRepository
from tools import load_builtin_tools
from tools.registry import registry
from skills.loader import configure_limits


async def run_async(*, execution_mode: str | None = None) -> None:
    settings = AppSettings.load(execution_mode=execution_mode)
    configure_limits(
        content_max_bytes=settings.skills.content_max_bytes,
        resource_max_bytes=settings.skills.resource_max_bytes,
    )
    load_builtin_tools()

    client = create_client(settings)
    repository = ThreadRepository(
        settings.runtime.state_dir,
        settings.runtime.thread_id,
        event_log_max_bytes=settings.runtime.event_log_max_bytes,
        event_log_backups=settings.runtime.event_log_backups,
        lease_ttl_seconds=settings.runtime.lease_ttl_seconds,
    )
    emit = lambda message: print(message, flush=True)
    approval_manager = ApprovalManager(emit=emit, channel_available=True)
    try:
        agent = AgentRunner(
            client=client,
            settings=settings,
            registry=registry,
            repository=repository,
            emit=emit,
            approval_manager=approval_manager,
        )
    except Exception:
        await client.close()
        raise
    await run_console(agent, client)


def create_client(settings: AppSettings):
    from openai import AsyncOpenAI

    client_kwargs = {
        "api_key": settings.model.api_key or None,
        "timeout": settings.runtime.model_timeout_seconds,
        "max_retries": 0,
    }
    if settings.model.base_url:
        client_kwargs["base_url"] = settings.model.base_url
    return AsyncOpenAI(**client_kwargs)


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Q-subagent")
    parser.add_argument(
        "--execution-mode",
        choices=("unrestricted", "approval"),
        help="覆盖 config.yaml 中的 powershell.mode",
    )
    arguments = parser.parse_args(argv)
    asyncio.run(run_async(execution_mode=arguments.execution_mode))
