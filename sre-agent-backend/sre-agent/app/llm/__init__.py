"""LLM 抽象及 Gateway 实现的稳定公共入口。"""

from app.llm.base import LLM, LLMMessage, LLMResponse
from app.llm.gateway import GatewayConfigurationError, GatewayLLM, GatewayRequestError

# 调用方只需依赖这些领域对象，不需要了解具体模块文件名。
__all__ = [
    "GatewayConfigurationError",
    "GatewayLLM",
    "GatewayRequestError",
    "LLM",
    "LLMMessage",
    "LLMResponse",
]
