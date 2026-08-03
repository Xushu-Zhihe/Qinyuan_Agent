from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ApprovalRequest:
    request_id: str
    command: str
    decision: asyncio.Future[bool]


class ApprovalManager:
    def __init__(
        self,
        *,
        emit: Callable[[str], None] = print,
        channel_available: bool = True,
    ) -> None:
        self.emit = emit
        self.channel_available = channel_available
        self._pending: dict[str, ApprovalRequest] = {}

    async def request(
        self,
        command: str,
        cancel_event: asyncio.Event,
    ) -> bool:
        if not self.channel_available:
            return False
        request_id = uuid.uuid4().hex[:12]
        future = asyncio.get_running_loop().create_future()
        request = ApprovalRequest(request_id, command, future)
        self._pending[request_id] = request
        self.emit(f"[审批请求] id={request_id}")
        self.emit(command)
        self.emit(f"输入 /approve {request_id} 或 /deny {request_id}")
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                return False
            if future.cancelled():
                return False
            return bool(future.result())
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
            self._pending.pop(request_id, None)

    def decide(self, request_id: str, approved: bool) -> bool:
        request = self._pending.get(request_id)
        if request is None or request.decision.done():
            return False
        request.decision.set_result(approved)
        return True

    def cancel_all(self) -> None:
        for request in list(self._pending.values()):
            if not request.decision.done():
                request.decision.cancel()
        self._pending.clear()

    def pending_ids(self) -> list[str]:
        return list(self._pending)
