"""Provider Adapter 的统一接口与结果模型。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.gateway.schema import ChatCompletionRequest


class ProviderError(Exception):
    """所有 Provider 调用异常的基类。"""


class ProviderConfigurationError(ProviderError):
    """Provider 缺少 API Key 等必要配置。"""


class ProviderRequestError(ProviderError):
    """Provider 网络请求或响应格式异常。"""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """各厂商响应转换后的统一内部结果。"""

    response_id: str
    model: str
    content: str
    finish_reason: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class BaseProviderAdapter(ABC):
    """所有厂商 Adapter 必须实现的异步接口。"""

    provider_name: str

    @abstractmethod
    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderResult:
        """把统一请求翻译为厂商请求，并把厂商响应翻译回来。"""

