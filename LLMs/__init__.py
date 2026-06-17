"""
LLM Adapters for Qinyuan_Agent

提供多种LLM后端支持：
- Kimi (Moonshot)
- DeepSeek
- OpenAI
"""

from LLMs.kimi_adapter import KimiChat, get_kimi_llm

__all__ = ["KimiChat", "get_kimi_llm"]
