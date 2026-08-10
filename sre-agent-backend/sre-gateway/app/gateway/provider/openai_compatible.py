"""OpenAI Chat Completions 兼容厂商的共享 Adapter。"""

import uuid

import httpx

from app.gateway.provider.base import (
    BaseProviderAdapter,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderResult,
)
from app.gateway.schema import ChatCompletionRequest


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """实现 OpenAI 与 DeepSeek 共用的请求/响应转换。"""

    def __init__(
        self,
        provider_name: str,
        provider_api_key: str | None,
        base_url: str,
        timeout: float,
    ) -> None:
        self.provider_name = provider_name
        # Provider API Key 来自服务端环境变量，绝不能使用客户端 gw_sk_ Key。
        self.provider_api_key = provider_api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderResult:
        """调用兼容的 ``/chat/completions`` 非流式接口。"""
        if not self.provider_api_key:
            raise ProviderConfigurationError(
                f"{self.provider_name} API key is not configured"
            )

        payload: dict = {
            "model": model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.provider_api_key}"
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderRequestError(
                f"{self.provider_name} returned HTTP {exc.response.status_code}",
                exc.response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderRequestError(f"{self.provider_name} request failed") from exc

        try:
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            return ProviderResult(
                response_id=data.get("id", f"gw-{uuid.uuid4().hex}"),
                model=data.get("model", model),
                content=choice["message"].get("content") or "",
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderRequestError(
                f"{self.provider_name} returned an invalid response"
            ) from exc
