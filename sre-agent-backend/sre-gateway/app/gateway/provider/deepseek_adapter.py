"""DeepSeek Provider Adapter。"""

from app.gateway.provider.openai_compatible import OpenAICompatibleAdapter


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """利用 DeepSeek 的 Chat Completions 兼容协议完成转换。"""

    def __init__(
        self, provider_api_key: str | None, base_url: str, timeout: float
    ) -> None:
        super().__init__("deepseek", provider_api_key, base_url, timeout)
