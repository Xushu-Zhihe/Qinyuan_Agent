from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. SkillAgentFactory（现场编译子Agent）
# ============================================================
class ToolCallingAgentRunner:
    """
    一个轻量级 LangChain Tool Calling Agent。

    这个类不负责创建沙盒。
    Agent 的工具在创建 Runner 之前就已经绑定到了专属 agent_id。
    """

    def __init__(
        self,
        llm: Any,
        system_prompt: str,
        tools: Sequence[StructuredTool],
        *,
        max_steps: int = 10,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")

        self.system_prompt = system_prompt
        self.max_steps = max_steps

        self.tools = list(tools)
        self.tools_map = {
            tool.name: tool
            for tool in self.tools
        }

        self.llm = (
            llm.bind_tools(self.tools)
            if self.tools
            else llm
        )

    @staticmethod
    def _serialize_content(value: Any) -> str:
        """
        LangChain ToolMessage 的 content 必须是字符串。

        MCP Tool 返回的是 dict，因此统一转成 JSON。
        """

        if isinstance(value, str):
            return value

        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
        )

    @staticmethod
    def _final_content(response: Any) -> str:
        """统一处理不同模型返回的 content 类型。"""

        content = getattr(response, "content", "")

        if isinstance(content, str):
            return content

        return json.dumps(
            content,
            ensure_ascii=False,
            default=str,
        )

    def _initial_messages(self, input_data: str) -> list[Any]:
        if not isinstance(input_data, str) or not input_data.strip():
            raise ValueError("Agent input cannot be empty")

        return [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=input_data.strip()),
        ]

    def run(self, input_data: str) -> str:
        """
        同步执行 Agent。

        如果 Agent 使用 MCP 同步 Tool，该方法必须运行在 MCPManager
        事件循环之外的线程中。
        """

        messages = self._initial_messages(input_data)

        for _ in range(self.max_steps):
            response = self.llm.invoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []

            if not tool_calls:
                return self._final_content(response)

            for tool_call in tool_calls:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})
                tool_call_id = tool_call.get("id", "")

                tool = self.tools_map.get(tool_name)

                if tool is None:
                    result: Any = {
                        "ok": False,
                        "status": "failed",
                        "message": f"Unknown tool: {tool_name}",
                    }
                else:
                    try:
                        result = tool.invoke(tool_args)
                    except Exception as error:
                        # 工具异常返回 Agent，不回退到宿主机 subprocess。
                        result = {
                            "ok": False,
                            "status": "failed",
                            "error": type(error).__name__,
                            "message": str(error),
                        }

                messages.append(
                    ToolMessage(
                        content=self._serialize_content(result),
                        tool_call_id=tool_call_id,
                    )
                )

        raise RuntimeError(
            f"Agent exceeded maximum step count: {self.max_steps}"
        )

    async def arun(self, input_data: str) -> str:
        """
        异步执行 Agent。

        调用方应优先使用这个入口，这样多个 subagent 可以并发运行，
        并直接调用 StructuredTool 的异步 coroutine。
        """

        messages = self._initial_messages(input_data)

        for _ in range(self.max_steps):
            response = await self.llm.ainvoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []

            if not tool_calls:
                return self._final_content(response)

            for tool_call in tool_calls:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})
                tool_call_id = tool_call.get("id", "")

                tool = self.tools_map.get(tool_name)

                if tool is None:
                    result: Any = {
                        "ok": False,
                        "status": "failed",
                        "message": f"Unknown tool: {tool_name}",
                    }
                else:
                    try:
                        result = await tool.ainvoke(tool_args)
                    except Exception as error:
                        result = {
                            "ok": False,
                            "status": "failed",
                            "error": type(error).__name__,
                            "message": str(error),
                        }

                messages.append(
                    ToolMessage(
                        content=self._serialize_content(result),
                        tool_call_id=tool_call_id,
                    )
                )

        raise RuntimeError(
            f"Agent exceeded maximum step count: {self.max_steps}"
        )

    def invoke(self, inputs: dict[str, Any]) -> dict[str, str]:
        """兼容部分 LangChain Runner 的同步接口。"""

        input_data = inputs.get("input", "")
        return {"output": self.run(str(input_data))}

    async def ainvoke(
        self,
        inputs: dict[str, Any],
    ) -> dict[str, str]:
        """兼容部分 LangChain Runner 的异步接口。"""

        input_data = inputs.get("input", "")
        return {"output": await self.arun(str(input_data))}

        
class SkillAgentFactory:
    def __init__(
        self,
        llm: Any,
        tool_registry: Optional[
            Dict[str, StructuredTool]
        ] = None,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry or {}
    
    def load_skill_md(self, skill_name: str) -> str:
        if not skill_name or Path(skill_name).name != skill_name:
            raise ValueError("skill_name must be a directory name")
        path = PROJECT_ROOT / "Skills" / skill_name / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"Skill '{skill_name}' not found: {path}")
        return path.read_text(encoding="utf-8")

    def create_from_prompt(
        self,
        system_prompt: str,
        *,
        tools: Optional[Sequence[str]] = None,
        max_steps: int = 10,
    ) -> ToolCallingAgentRunner:
        """
        直接根据系统提示词创建 Agent。

        当前 CLI 使用这个入口，因此暂时不必额外创建大量 SKILL.md 文件。
        """

        if not system_prompt.strip():
            raise ValueError("system_prompt cannot be empty")

        selected_tools: list[StructuredTool] = []

        for tool_name in tools or ():
            tool = self.tool_registry.get(tool_name)

            if tool is None:
                raise KeyError(
                    f"Tool {tool_name!r} is not registered"
                )

            selected_tools.append(tool)

        return ToolCallingAgentRunner(
            llm=self.llm,
            system_prompt=system_prompt.strip(),
            tools=selected_tools,
            max_steps=max_steps,
        )
    
    def create(
        self,
        skill_name: str,
        tools: Optional[Sequence[str]] = None,
        max_steps: int = 10,
    ) -> ToolCallingAgentRunner:
        """根据 SKILL.md 创建 Agent。"""

        return self.create_from_prompt(
            self.load_skill_md(skill_name),
            tools=tools,
            max_steps=max_steps,
        )


# ============================================================
# 2. MetaAgent（主Agent：spawn_agent 拦截式）
# ============================================================

class SpawnAgentArgs(BaseModel):
    skill_name: str = Field(description="Skills目录下的skill名称")
    task_description: str = Field(description="子Agent的任务描述")
    input_data: str = Field(description="传给子Agent的输入")
    required_tools: List[str] = Field(default_factory=list, description="需要的工具名列表")


@dataclass
class _SessionState:
    messages: List[dict] = field(default_factory=list)
    status: str = "IDLE"  # IDLE / RUNNING / WAITING_APPROVAL
    pending_proposal: Optional[dict] = None


class MetaAgent:
    SYSTEM_PROMPT = """你是一个智能任务调度中枢。

你的职责：
1. 理解用户的复杂需求
2. 如果需求简单，直接回答
3. 如果需要专业技能，调用 spawn_agent 工具提议创建临时子Agent
4. 收到子Agent结果后，整合分析并继续

可用的 Skill 目录：
{skill_list}

调用 spawn_agent 的规则：
- skill_name 必须是上面列出的目录名
- task_description 要清晰说明任务
- input_data 是传给子Agent的初始指令
- required_tools 只填该 skill 实际需要的工具名

注意：spawn_agent 执行后，你会收到子Agent的结果，请基于真实结果继续。
如果用户拒绝了提议，请重新规划。
"""
    
    def __init__(self, llm, factory: SkillAgentFactory, available_skills: List[str]):
        self.factory = factory
        
        # spawn_agent 工具定义
        self.spawn_tool = StructuredTool.from_function(
            func=lambda **kwargs: kwargs,  # 占位，不会真执行
            name="spawn_agent",
            description="当你需要创建临时专业子Agent来完成某个子任务时调用此工具。",
            args_schema=SpawnAgentArgs,
        )
        
        # LLM 绑定 spawn_agent 工具
        self.llm = llm.bind_tools([self.spawn_tool])
        
        skill_list_str = "\n".join([f"- {s}" for s in available_skills])
        self.system_content = self.SYSTEM_PROMPT.format(skill_list=skill_list_str)
        self._sessions: Dict[str, _SessionState] = {}
    
    # ----- 辅助：会话管理 -----
    
    def _get_session(self, sid: str) -> _SessionState:
        if sid not in self._sessions:
            self._sessions[sid] = _SessionState()
        return self._sessions[sid]
    
    def _build_lc_messages(
        self,
        sess: _SessionState,
    ) -> List:
        """把内部消息历史转换为 LangChain Message。"""

        messages = [
            SystemMessage(content=self.system_content)
        ]

        for message in sess.messages:
            role = message.get("role")
            content = message.get("content", "")

            if role == "human":
                messages.append(
                    HumanMessage(content=content)
                )

            elif role == "ai":
                tool_calls = message.get("tool_calls")

                if tool_calls:
                    messages.append(
                        AIMessage(
                            content=content,
                            tool_calls=tool_calls,
                        )
                    )
                else:
                    messages.append(
                        AIMessage(content=content)
                    )

            elif role == "tool":
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=message.get(
                            "tool_call_id",
                            "",
                        ),
                    )
                )

        return messages
    
    def _find_last_spawn_tool_call_id(self, sess: _SessionState) -> str:
        """从最近一条含 tool_calls 的 AI 消息里，找到 spawn_agent 的 call_id"""
        for m in reversed(sess.messages):
            if m.get("role") == "ai" and "tool_calls" in m:
                for tc in m["tool_calls"]:
                    if tc.get("name") == "spawn_agent":
                        return tc.get("id", "")
        return ""
    
    # ----- 核心三个入口 -----
    
    def run(self, session_id: str, user_input: str) -> dict:
        """
        主入口。返回 dict：
        - {"type": "final", "content": "..."}
        - {"type": "proposal", "data": {...}}
        """
        sess = self._get_session(session_id)
        sess.status = "RUNNING"
        sess.messages.append({"role": "human", "content": user_input})
        
        msgs = self._build_lc_messages(sess)
        response = self.llm.invoke(msgs)
        
        # 检查是否触发了 spawn_agent
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc["name"] == "spawn_agent":
                    proposal = tc["args"]
                    sess.status = "WAITING_APPROVAL"
                    sess.pending_proposal = proposal
                    
                    # 把 AI 回复（含 tool_calls）存历史，resume 时需要 tool_call_id
                    sess.messages.append({
                        "role": "ai",
                        "content": response.content or "",
                        "tool_calls": response.tool_calls
                    })
                    return {
                        "type": "proposal",
                        "data": proposal,
                        "reason": f"主Agent请求创建 {proposal['skill_name']} 子Agent"
                    }
        
        # 没有 spawn，直接输出
        sess.messages.append({"role": "ai", "content": response.content})
        sess.status = "IDLE"
        return {"type": "final", "content": response.content}
    
    def resume_with_approval(self, session_id: str, user_decision: Optional[dict] = None) -> dict:
        """
        用户批准后恢复。user_decision 可覆盖原 proposal 的参数。
        """
        sess = self._get_session(session_id)
        proposal = sess.pending_proposal
        if not proposal:
            return {"type": "error", "msg": "当前没有待批准的提议"}
        
        # 解析最终参数（用户可能编辑过）
        skill_name = user_decision.get("skill_name", proposal["skill_name"]) if user_decision else proposal["skill_name"]
        input_data = user_decision.get("input_data", proposal["input_data"]) if user_decision else proposal["input_data"]
        tools = user_decision.get("required_tools", proposal.get("required_tools", [])) if user_decision else proposal.get("required_tools", [])
        
        # 现场生成并运行子Agent
        sub_agent = self.factory.create(skill_name, tools=tools)
        result = sub_agent.run(input_data)
        
        # 构造 ToolMessage 塞回主Agent历史
        tool_call_id = self._find_last_spawn_tool_call_id(sess)
        sess.messages.append({
            "role": "tool",
            "content": f"[子Agent {skill_name} 执行结果]\n{result}",
            "tool_call_id": tool_call_id
        })
        sess.pending_proposal = None
        
        # 触发主Agent继续
        return self.run(session_id, "请继续")
    
    def resume_with_rejection(self, session_id: str, reason: str = "") -> dict:
        """
        用户拒绝后恢复，让主Agent重新规划。
        """
        sess = self._get_session(session_id)
        
        tool_call_id = self._find_last_spawn_tool_call_id(sess)
        sess.messages.append({
            "role": "tool",
            "content": f"[用户拒绝] 原因：{reason or '未说明'}。请重新评估并调整计划。",
            "tool_call_id": tool_call_id
        })
        sess.pending_proposal = None
        
        return self.run(session_id, "请重新规划")
    
    # ----- 辅助方法 -----
    
    def get_pending_proposal(self, session_id: str) -> Optional[dict]:
        sess = self._sessions.get(session_id)
        if sess and sess.status == "WAITING_APPROVAL":
            return sess.pending_proposal
        return None
    
    def reset_session(self, session_id: str):
        self._sessions[session_id] = _SessionState()
