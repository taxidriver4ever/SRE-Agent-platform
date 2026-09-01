"""vLLM OpenAI-Compatible Server Provider Adapter。"""

from dataclasses import replace

from app.gateway.provider.openai_compatible import OpenAICompatibleAdapter
from app.gateway.provider.base import ProviderResult
from app.gateway.schema import ChatCompletionRequest


class VllmAdapter(OpenAICompatibleAdapter):
    """通过 vLLM 的 ``/v1/chat/completions`` 接口执行本地推理。"""

    provider_name = "vllm"

    def __init__(
        self, provider_api_key: str | None, base_url: str, timeout: float
    ) -> None:
        # vLLM 与 OpenAI 共用协议，但保持独立 Provider 名称、密钥和审计维度。
        super().__init__(self.provider_name, provider_api_key, base_url, timeout)

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderResult:
        """调用 vLLM，并防御性移除模型模板偶尔泄漏的思考前缀。"""
        result = await super().complete(request, model)
        return replace(result, content=_without_thinking(result.content))


def _without_thinking(content: str) -> str:
    """保留最后一个 ``</think>`` 后的最终回答；普通文本保持原样。"""
    if "</think>" in content:
        return content.rsplit("</think>", 1)[1].strip()
    return content.strip()
