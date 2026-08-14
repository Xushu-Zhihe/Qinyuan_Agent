"""
Kimi Adapter for Qinyuan_Agent



Kimi支持OpenAI兼容的API，因此可以通过ChatOpenAI进行调用。
"""

from typing import Optional, Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel
import os


class KimiChat(ChatOpenAI):
    """
    Kimi Chat LLM 适配器
    
    基于OpenAI兼容API实现，支持Moonshot Kimi模型。
    
    环境变量配置：
    - KIMI_API_KEY: Kimi API密钥
    - KIMI_BASE_URL: Kimi API基础URL（默认: https://api.moonshot.cn/v1）
    - KIMI_MODEL: Kimi模型名称（默认: moonshot-v1-8k）
    
    支持的模型：
    - moonshot-v1-8k: 8k上下文
    - moonshot-v1-32k: 32k上下文
    - moonshot-v1-128k: 128k上下文
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.moonshot.cn/v1",
        model: str = "moonshot-v1-8k",
        **kwargs
    ):
        api_key = api_key or os.environ.get("KIMI_API_KEY")
        if not api_key:
            raise ValueError(
                "KIMI_API_KEY 环境变量未设置。"
                "请设置环境变量或在初始化时传入api_key参数。"
            )
        
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            **kwargs
        )


def get_kimi_llm(**kwargs) -> BaseChatModel:
    """
    获取Kimi LLM实例
    
    Args:
        **kwargs: 传递给KimiChat的参数
    
    Returns:
        BaseChatModel: Kimi LLM实例
    
    Example:
        >>> from LLMs.kimi_adapter import get_kimi_llm
        >>> llm = get_kimi_llm(model="moonshot-v1-32k")
        >>> response = llm.invoke("你好")
    """
    return KimiChat(**kwargs)


# 示例用法
if __name__ == "__main__":
    # 设置环境变量（实际使用时应通过.env文件设置）
    os.environ["KIMI_API_KEY"] = "your-kimi-api-key"
    
    # 使用API模式
    kimi_api = KimiChat(model="moonshot-v1-8k")
    response = kimi_api.invoke("你好，我是Kimi用户")
    print(f"API模式响应: {response.content}")
