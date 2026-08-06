from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from Agents.agent import SkillAgentFactory
from MCPs.Mcp_client import MCPManager
from Protocol.models import (
    ConcludeResponse,
    Intent,
)
from Sandbox.models import SandboxSession
from Sandbox.service import SandboxService
from Tools import create_agent_tool_registry


class BlackboardPort(Protocol):
    """
    BlackboardWorker 所需要的最小黑板接口。
    """

    async def claim_intent(
        self,
        project_id: str,
        intent_id: str,
        *,
        worker: str,
        trigger: str,
    ) -> Intent:
        """使用 worker ID 认领一个 Intent。"""
        ...

    async def heartbeat_intent(
        self,
        project_id: str,
        intent_id: str,
        *,
        worker: str,
    ) -> Intent:
        """为已经认领的 Intent 续约。"""
        ...

    async def conclude_intent(
        self,
        project_id: str,
        intent_id: str,
        *,
        worker: str,
        description: str,
    ) -> ConcludeResponse:
        """结束 Intent，并将 Agent 结论写入新的 Fact。"""
        ...


class WorkerLeaseError(RuntimeError):
    """Worker 无法认领 Intent 或丢失黑板租约。"""


@dataclass(frozen=True, slots=True)
class WorkerJob:
    """Orchestrator 分配给一个 Blackboard Worker 的工作。"""

    project_id: str
    intent_id: str
    target: str

    # 黑板 Fact、Hint、父 Intent 等上下文可以整理成字符串传入。
    context: str = ""

    # 记录本次认领是由哪个调度行为触发的。
    trigger: str = "orchestrator-dispatch"

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "intent_id",
            "target",
            "trigger",
        ):
            value = getattr(self, field_name)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{field_name} cannot be empty"
                )


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """一个 Worker 完成 Intent 后的结果摘要。"""

    worker_id: str
    project_id: str
    intent_id: str
    sandbox_id: str
    result: str
    fact_id: str


BLACKBOARD_WORKER_PROMPT = """
你是黑板架构中的一个独立安全测试 Worker。

你不是固定的 Recon Agent、Analyzer Agent 或 Exploit Agent。
你的具体职责由当前认领到的 Blackboard Intent 决定。

安全规则：
1. 只能测试任务中明确给出的授权目标。
2. 当前只能使用分配给你的 nmap_tcp_scan 工具。
3. 不得假设自己能够调用宿主机 Shell 或 subprocess。
4. 不得把 agent_id、worker_id 或 sandbox_id 作为工具参数。
5. 工具返回失败时应说明失败，不得伪造扫描结果。
6. 最终回答必须区分事实、推断和仍需验证的内容。
7. 当前阶段只执行信息收集和端口验证，不执行破坏性攻击。

完成任务后，返回一段适合写入 Blackboard Fact 的客观结论。
""".strip()


class BlackboardWorker:
    """
    一个 Blackboard Worker 的完整运行时。

    生命周期：

    认领 Intent
        → 使用 worker_id 创建专属 Sandbox
        → 使用相同 worker_id 绑定 MCP Client
        → 创建专属 Nmap Tool
        → 执行 Agent
        → 写回 Blackboard Fact
        → 删除专属 Sandbox
    """

    def __init__(
        self,
        *,
        worker_id: str,
        llm: Any,
        blackboard: BlackboardPort,
        sandbox_service: SandboxService,
        mcp_manager: MCPManager,
        heartbeat_interval_seconds: float = 15.0,
        maximum_agent_steps: int = 6,
    ) -> None:
        normalized_worker_id = worker_id.strip()

        if not normalized_worker_id:
            raise ValueError("worker_id cannot be empty")

        if heartbeat_interval_seconds <= 0:
            raise ValueError(
                "heartbeat_interval_seconds must be positive"
            )

        if maximum_agent_steps < 1:
            raise ValueError(
                "maximum_agent_steps must be positive"
            )

        # 最重要的绑定关系：
        # Blackboard worker == Agent agent_id == Sandbox owner。
        self.worker_id = normalized_worker_id

        self.llm = llm
        self.blackboard = blackboard
        self.sandbox_service = sandbox_service
        self.mcp_manager = mcp_manager

        self.heartbeat_interval_seconds = (
            heartbeat_interval_seconds
        )
        self.maximum_agent_steps = maximum_agent_steps

    async def run(self, job: WorkerJob) -> WorkerOutcome:
        """认领并执行一个 Blackboard Intent。"""

        claimed_intent = await self.blackboard.claim_intent(
            job.project_id,
            job.intent_id,
            worker=self.worker_id,
            trigger=job.trigger,
        )

        if claimed_intent.id != job.intent_id:
            raise WorkerLeaseError(
                "Blackboard returned a different intent"
            )

        if claimed_intent.worker != self.worker_id:
            raise WorkerLeaseError(
                f"Intent {job.intent_id!r} was not assigned to "
                f"worker {self.worker_id!r}"
            )

        stop_heartbeat = asyncio.Event()

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                job=job,
                stop_event=stop_heartbeat,
            ),
            name=f"heartbeat-{self.worker_id}",
        )

        sandbox_session: SandboxSession | None = None
        agent_task: asyncio.Task[str] | None = None

        try:
            # Docker SDK 是同步接口，放入线程，避免阻塞 asyncio。
            sandbox_session = await asyncio.to_thread(
                self.sandbox_service.start_for_agent,
                self.worker_id,
            )

            # 这里生成的工具闭包已经永久绑定 self.worker_id。
            # LLM 看不到也不能修改 worker_id。
            tool_registry = create_agent_tool_registry(
                self.worker_id,
                self.mcp_manager,
            )

            factory = SkillAgentFactory(
                llm=self.llm,
                tool_registry=tool_registry,
            )

            agent = factory.create_from_prompt(
                BLACKBOARD_WORKER_PROMPT,
                tools=["nmap_tcp_scan"],
                max_steps=self.maximum_agent_steps,
            )

            agent_input = self._build_agent_input(
                job=job,
                intent=claimed_intent,
            )

            agent_task = asyncio.create_task(
                agent.arun(agent_input),
                name=f"agent-{self.worker_id}",
            )

            # 如果 heartbeat 提前失败，立即终止 Agent，避免在租约失效后
            # 继续产生重复或冲突的黑板结论。
            completed, _ = await asyncio.wait(
                {agent_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if heartbeat_task in completed:
                heartbeat_error = heartbeat_task.exception()

                if agent_task is not None:
                    agent_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await agent_task

                if heartbeat_error is not None:
                    raise WorkerLeaseError(
                        "Blackboard heartbeat failed"
                    ) from heartbeat_error

                raise WorkerLeaseError(
                    "Blackboard heartbeat stopped unexpectedly"
                )

            result = agent_task.result()

            conclusion = await self.blackboard.conclude_intent(
                job.project_id,
                job.intent_id,
                worker=self.worker_id,
                description=result,
            )

            return WorkerOutcome(
                worker_id=self.worker_id,
                project_id=job.project_id,
                intent_id=job.intent_id,
                sandbox_id=sandbox_session.sandbox_id,
                result=result,
                fact_id=conclusion.fact.id,
            )

        finally:
            stop_heartbeat.set()

            if agent_task is not None and not agent_task.done():
                agent_task.cancel()
                with suppress(asyncio.CancelledError):
                    await agent_task

            if not heartbeat_task.done():
                with suppress(
                    asyncio.CancelledError,
                    Exception,
                ):
                    await heartbeat_task

            # 一个 Worker 完成或失败后，只删除自己的容器。
            if sandbox_session is not None:
                await asyncio.to_thread(
                    self.sandbox_service.stop_for_agent,
                    self.worker_id,
                )

    async def _heartbeat_loop(
        self,
        *,
        job: WorkerJob,
        stop_event: asyncio.Event,
    ) -> None:
        """在 Worker 执行期间定期续约黑板 Intent。"""

        while True:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return

            except TimeoutError:
                await self.blackboard.heartbeat_intent(
                    job.project_id,
                    job.intent_id,
                    worker=self.worker_id,
                )

    @staticmethod
    def _build_agent_input(
        *,
        job: WorkerJob,
        intent: Intent,
    ) -> str:
        """将黑板 Intent 转换成 Agent 输入。"""

        context = job.context.strip() or "没有额外上下文。"

        return (
            f"Blackboard Intent ID: {intent.id}\n"
            f"授权目标: {job.target}\n"
            f"Intent 描述: {intent.description}\n"
            f"相关上下文:\n{context}\n\n"
            "请判断是否需要调用 nmap_tcp_scan。"
            "如果调用，只能扫描上述授权目标。"
            "最后输出适合写回黑板 Fact 的客观结论。"
        )