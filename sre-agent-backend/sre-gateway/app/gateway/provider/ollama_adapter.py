"""Ollama 本地 Chat API Provider Adapter。"""

import uuid

import httpx

from app.gateway.provider.base import (
    BaseProviderAdapter,
    ProviderRequestError,
    ProviderResult,
)
from app.gateway.schema import ChatCompletionRequest


class OllamaAdapter(BaseProviderAdapter):
    """把统一请求翻译为 Ollama ``/api/chat`` 格式。"""

    provider_name = "ollama"

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderResult:
        """调用本机 Ollama，并读取其原生 Token 统计字段。"""
        payload: dict = {
            "model": model,
            "messages": [message.model_dump() for message in request.messages],
            # Ollama 默认流式返回；Gateway 当前需要完整 JSON，所以必须关闭。
            "stream": False,
            # Qwen3 默认可能进入思考模式。Agent 要求 content 中只包含决策 JSON，
            # 因此关闭独立思考输出，减少延迟并提高协议解析稳定性。
            "think": False,
        }
        options = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if options:
            payload["options"] = options

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderRequestError(
                f"ollama returned HTTP {exc.response.status_code}",
                exc.response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderRequestError("ollama request failed") from exc

        try:
            return ProviderResult(
                response_id=f"ollama-{uuid.uuid4().hex}",
                model=data.get("model", model),
                # 部分 Qwen3 模型即使 think=false 仍会把内部推理放在 content
                # 并以 </think> 结束。统一清理后，上层 Agent 才能稳定解析 JSON。
                content=_without_thinking(data["message"].get("content") or ""),
                finish_reason=data.get("done_reason"),
                prompt_tokens=int(data.get("prompt_eval_count", 0)),
                completion_tokens=int(data.get("eval_count", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRequestError("ollama returned an invalid response") from exc


def _without_thinking(content: str) -> str:
    """移除 Qwen 系列偶尔混入 ``content`` 的内部思考段。

    新版 Ollama 通常把 thinking 和 content 分字段返回，但某些模型模板会直接
    输出 ``...思考内容...</think>最终回答``，甚至省略开标签。保留最后一个
    ``</think>`` 之后的文本可以兼容这两种情况；普通回答保持原样。
    """
    if "</think>" in content:
        return content.rsplit("</think>", 1)[1].strip()
    return content.strip()
