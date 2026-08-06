# from langchain_openai import ChatOpenAI
# from dotenv import load_dotenv
# from Agents.agent import *
# import os
# # 核心层

# load_dotenv()

# LLM=ChatOpenAI(model="XXXXX")#测试选用deepseek，实战用gpt
# #Embedding=OpenAIEmbeddings(model="XXXXX")嵌入式模型，RAG用到，暂时不考虑


# #状态定义
# # class PentestState(TypedDict):
# #     messages: Annotated[Sequence[BaseMessage], operator.add]  # ← 所有人的对话记录
# #     target: str                                                # ← 目标地址
# #     current_step: str                                          # ← 现在轮到谁干活
# #     recon_data: dict                                           # ← 侦察结果栏
# #     vuln_report: list                                          # ← 漏洞发现栏
# #     exploit_results: list                                      # ← 渗透成果栏
# #     final_report: str                                          # ← 最终报告栏

# #RAG初始化


# #Agent定义
# # 侦查agent
# # 漏洞分析agent
# # 渗透测试agent
# # 复盘wpagent

# #工作流

# #入口

# def run_pentest(target: str):
#     """自动化渗透测试入口"""
#     # return app.invoke({...})
#     pass
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Sequence

from Agents.worker import (
    BlackboardPort,
    BlackboardWorker,
    WorkerJob,
    WorkerOutcome,
)
from MCPs.Mcp_client import (
    MCPManager,
    build_mcp_runtime,
)
from Sandbox.docker_runtime import DockerRuntime
from Sandbox.service import SandboxService


class PentestOrchestrator:
    """
    管理整个多 Agent 运行时。

    MCP Server 是共享的：
        不需要为每个 Agent 启动一个 Nmap MCP Server。

    Sandbox 是独立的：
        每个 Blackboard Worker 各自拥有一个容器。

    Tool 是专属绑定的：
        每个 Worker 都有一个绑定自己 worker_id 的 Tool 闭包。
    """

    def __init__(
        self,
        *,
        llm: Any,
        blackboard: BlackboardPort,
        sandbox_service: SandboxService,
        mcp_manager: MCPManager,
    ) -> None:
        self.llm = llm
        self.blackboard = blackboard
        self.sandbox_service = sandbox_service
        self.mcp_manager = mcp_manager
        self._closed = False

    @classmethod
    async def create(
        cls,
        *,
        llm: Any,
        blackboard: BlackboardPort,
    ) -> "PentestOrchestrator":
        """
        创建 Docker Runtime、SandboxService 和共享 MCPManager。

        Docker 初始化是同步 I/O，因此放入工作线程。
        """

        runtime = await asyncio.to_thread(DockerRuntime)
        sandbox_service = SandboxService(runtime)

        try:
            mcp_manager = await build_mcp_runtime(
                sandbox_service
            )

        except Exception:
            await asyncio.to_thread(
                sandbox_service.close
            )
            raise

        return cls(
            llm=llm,
            blackboard=blackboard,
            sandbox_service=sandbox_service,
            mcp_manager=mcp_manager,
        )

    async def run_job(
        self,
        job: WorkerJob,
        *,
        worker_id: str | None = None,
    ) -> WorkerOutcome:
        """
        启动一个真实 Blackboard Worker。

        如果没有传 worker_id，Orchestrator 生成一个全局唯一 ID。
        """

        if self._closed:
            raise RuntimeError(
                "PentestOrchestrator is closed"
            )

        actual_worker_id = (
            worker_id.strip()
            if worker_id is not None
            else self._generate_worker_id(job.intent_id)
        )

        if not actual_worker_id:
            raise ValueError("worker_id cannot be empty")

        worker = BlackboardWorker(
            worker_id=actual_worker_id,
            llm=self.llm,
            blackboard=self.blackboard,
            sandbox_service=self.sandbox_service,
            mcp_manager=self.mcp_manager,
        )

        return await worker.run(job)

    async def run_jobs(
        self,
        jobs: Sequence[WorkerJob],
    ) -> list[WorkerOutcome]:
        """
        并发运行多个 Blackboard Worker。

        每个 job 都会获得：
        - 不同 worker_id
        - 不同 Docker 容器
        - 绑定自身 worker_id 的 Nmap Tool
        """

        if not jobs:
            return []

        return list(
            await asyncio.gather(
                *(
                    self.run_job(job)
                    for job in jobs
                )
            )
        )

    async def close(self) -> None:
        """按正确顺序关闭 MCP Server 和全部 Agent 容器。"""

        if self._closed:
            return

        self._closed = True
        errors: list[str] = []

        try:
            await self.mcp_manager.stop_all()
        except Exception as error:
            errors.append(
                f"failed to stop MCPManager: {error}"
            )

        try:
            await asyncio.to_thread(
                self.sandbox_service.close
            )
        except Exception as error:
            errors.append(
                f"failed to close SandboxService: {error}"
            )

        if errors:
            raise RuntimeError("; ".join(errors))

    async def __aenter__(self) -> "PentestOrchestrator":
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()

    @staticmethod
    def _generate_worker_id(intent_id: str) -> str:
        """
        生成 Worker ID。

        Intent ID 保留在名称中，便于 docker ps 和日志审计。
        UUID 防止同一 Intent 重试时发生名称冲突。
        """

        safe_intent_part = "".join(
            character
            if character.isalnum() or character in "-_"
            else "-"
            for character in intent_id
        ).strip("-_")

        safe_intent_part = safe_intent_part[:24] or "intent"

        return (
            f"worker-{safe_intent_part}-"
            f"{uuid.uuid4().hex[:8]}"
        )


async def run_pentest(
    *,
    target: str,
    project_id: str,
    intent_id: str,
    llm: Any,
    blackboard: BlackboardPort,
    context: str = "",
) -> WorkerOutcome:
    """
    当前阶段的单 Worker 自动化测试入口。

    后续 Orchestrator 从黑板获取多个未认领 Intent 后，
    可以改用 orchestrator.run_jobs() 并发执行。
    """

    orchestrator = await PentestOrchestrator.create(
        llm=llm,
        blackboard=blackboard,
    )

    try:
        return await orchestrator.run_job(
            WorkerJob(
                project_id=project_id,
                intent_id=intent_id,
                target=target,
                context=context,
            )
        )

    finally:
        await orchestrator.close()