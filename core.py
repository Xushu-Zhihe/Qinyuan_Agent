from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os
# 核心层

load_dotenv()

LLM=ChatOpenAI(model="XXXXX")#测试选用deepseek，实战用gpt
#Embedding=OpenAIEmbeddings(model="XXXXX")嵌入式模型，RAG用到，暂时不考虑


#状态定义
# class PentestState(TypedDict):
#     messages: Annotated[Sequence[BaseMessage], operator.add]  # ← 所有人的对话记录
#     target: str                                                # ← 目标地址
#     current_step: str                                          # ← 现在轮到谁干活
#     recon_data: dict                                           # ← 侦察结果栏
#     vuln_report: list                                          # ← 漏洞发现栏
#     exploit_results: list                                      # ← 渗透成果栏
#     final_report: str                                          # ← 最终报告栏

#RAG初始化


#Agent定义
# 侦查agent
# 漏洞分析agent
# 渗透测试agent
# 复盘wpagent

#工作流

#入口

def run_pentest(target: str):
    """自动化渗透测试入口"""
    # return app.invoke({...})
    pass