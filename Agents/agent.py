import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langchain_core.messages import (
    SystemMessage, HumanMessage, AIMessage, ToolMessage
)


# ============================================================
# 1. SkillAgentFactory（现场编译子Agent）
# ============================================================

class SkillAgentFactory:
    def __init__(self, llm, tool_registry: Optional[Dict[str, Any]] = None):
        self.llm = llm
        self.tool_registry = tool_registry or {}
    
    def load_skill_md(self, skill_name: str) -> str:
        path = Path("Skills") / skill_name / "SKILL.md"
        if not path.exists():
            raise FileNotFoundError(f"Skill '{skill_name}' not found: {path}")
        return path.read_text(encoding="utf-8")
    
    def create(self, skill_name: str, tools: Optional[List[str]] = None, max_steps: int = 10):
        """
        返回一个带 .run(input) 和 .invoke({"input": ...}) 方法的 runner 对象。
        内部自己写 tool calling loop，不依赖 AgentExecutor。
        """
        skill_md = self.load_skill_md(skill_name)
        
        # 解析工具
        tool_objs = []
        tools_map = {}
        for tname in (tools or []):
            if tname not in self.tool_registry:
                raise KeyError(f"Tool '{tname}' not registered")
            t = self.tool_registry[tname]
            tool_objs.append(t)
            tools_map[t.name] = t
        
        # LLM 绑定工具
        llm_with_tools = self.llm.bind_tools(tool_objs) if tool_objs else self.llm
        
        def _run(input_data: str) -> str:
            messages = [
                SystemMessage(content=skill_md),
                HumanMessage(content=input_data),
            ]
            
            for _ in range(max_steps):
                response = llm_with_tools.invoke(messages)
                messages.append(response)
                
                # 没有 tool_calls，说明已给出最终答案
                if not response.tool_calls:
                    return response.content
                
                # 执行工具，结果包成 ToolMessage 塞回
                for tc in response.tool_calls:
                    tname = tc["name"]
                    targs = tc["args"]
                    tid = tc["id"]
                    
                    if tname not in tools_map:
                        result = f"Error: unknown tool '{tname}'"
                    else:
                        result = tools_map[tname].invoke(targs)
                    
                    messages.append(ToolMessage(content=str(result), tool_call_id=tid))
            
            return "Error: max steps reached"
        
        # 包装成和 AgentExecutor 接口兼容的对象
        class _Runner:
            def __init__(self, fn):
                self._fn = fn
            def run(self, input_data: str) -> str:
                return self._fn(input_data)
            def invoke(self, inputs: dict) -> dict:
                inp = inputs.get("input", inputs)
                return {"output": self._fn(inp)}
        
        return _Runner(_run)


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
    
    def _build_lc_messages(self, sess: _SessionState, user_input: str) -> List:
        """把内部 dict 历史转成 LangChain Message 对象"""
        msgs = [SystemMessage(content=self.system_content)]
        for m in sess.messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "human":
                msgs.append(HumanMessage(content=content))
            elif role == "ai":
                # 如果有 tool_calls，需要还原 AIMessage 的 tool_calls 字段
                tcs = m.get("tool_calls")
                if tcs:
                    msgs.append(AIMessage(content=content, tool_calls=tcs))
                else:
                    msgs.append(AIMessage(content=content))
            elif role == "tool":
                msgs.append(ToolMessage(content=content, tool_call_id=m.get("tool_call_id", "")))
        msgs.append(HumanMessage(content=user_input))
        return msgs
    
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
        
        msgs = self._build_lc_messages(sess, user_input)
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