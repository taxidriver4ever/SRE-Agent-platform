"""Claude Messages API Provider Adapter。"""

import uuid

import httpx

from app.gateway.provider.base import (
    BaseProviderAdapter,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderResult,
)
from app.gateway.schema import ChatCompletionRequest


class ClaudeAdapter(BaseProviderAdapter):
    """把统一 Chat 请求翻译为 Anthropic Messages API 格式。"""

    provider_name = "claude"

    def __init__(
        self, provider_api_key: str | None, base_url: str, timeout: float
    ) -> None:
        # 厂商密钥仅在 Adapter 内使用，不接受客户端 gw_sk_ Key。
        self.provider_api_key = provider_api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderResult:
        """拆分 system 消息后调用 Claude ``/messages`` 接口。"""
        if not self.provider_api_key:
            raise ProviderConfigurationError("claude API key is not configured")

        system_parts = [
            message.content for message in request.messages if message.role == "system"
        ]
        messages = [
            message.model_dump()
            for message in request.messages
            if message.role != "system"
        ]
        if not messages:
            raise ProviderRequestError("claude requires a user or assistant message", 422)

        payload: dict = {
            "model": model,
            "messages": messages,
            # Claude Messages API 要求 max_tokens；统一协议未提供时使用保守默认值。
            "max_tokens": request.max_tokens or 1024,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/messages",
                    headers={
                        "x-api-key": self.provider_api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderRequestError(
                f"claude returned HTTP {exc.response.status_code}",
                exc.response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderRequestError("claude request failed") from exc

        try:
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            usage = data.get("usage") or {}
            return ProviderResult(
                response_id=data.get("id", f"gw-{uuid.uuid4().hex}"),
                model=data.get("model", model),
                content=text,
                finish_reason=data.get("stop_reason"),
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderRequestError("claude returned an invalid response") from exc
