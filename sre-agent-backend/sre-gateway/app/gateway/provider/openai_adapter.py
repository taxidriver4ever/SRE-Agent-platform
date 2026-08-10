"""OpenAI Provider Adapter。"""

from app.gateway.provider.openai_compatible import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    """把统一请求转换为 OpenAI Chat Completions 请求。"""

    def __init__(
        self, provider_api_key: str | None, base_url: str, timeout: float
    ) -> None:
        super().__init__("openai", provider_api_key, base_url, timeout)
