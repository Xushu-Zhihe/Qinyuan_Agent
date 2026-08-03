from __future__ import annotations

import asyncio
from typing import Any

from .agent import AgentRunner


async def run_console(agent: AgentRunner, client: Any) -> None:
    await agent.activate()
    try:
        while True:
            prompt = (await asyncio.to_thread(input, "> ")).strip()
            command = prompt.lower()

            if command in {"exit", "/exit"}:
                return
            if command in {"interrupt", "/interrupt"}:
                await agent.interrupt()
                continue
            if command in {"continue", "/continue"}:
                try:
                    agent.resume_turn()
                except RuntimeError as exc:
                    print(str(exc), flush=True)
                continue
            if command.startswith("/approve "):
                request_id = prompt.split(maxsplit=1)[1]
                print(
                    "审批已通过" if agent.approve(request_id) else "审批请求不存在",
                    flush=True,
                )
                continue
            if command.startswith("/deny "):
                request_id = prompt.split(maxsplit=1)[1]
                print(
                    "审批已拒绝" if agent.deny(request_id) else "审批请求不存在",
                    flush=True,
                )
                continue
            if not prompt:
                continue
            if agent.has_active_turn():
                print("当前已有任务；输入 /interrupt 可中断", flush=True)
                continue
            agent.start_turn(prompt)
    finally:
        await agent.close()
        await client.close()
