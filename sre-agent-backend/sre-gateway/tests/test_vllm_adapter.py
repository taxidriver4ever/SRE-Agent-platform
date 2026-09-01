"""vLLM OpenAI-Compatible Adapter 协议测试。"""

import asyncio
import json

import httpx
import pytest

from app.gateway.provider import ProviderConfigurationError, VllmAdapter
from app.gateway.provider.vllm_adapter import _without_thinking
from app.gateway.schema import ChatCompletionRequest


class _FakeAsyncClient:
    def __init__(self, handler, timeout: float) -> None:
        self.handler = handler
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
        return self.handler(url, headers, json)


def test_vllm_adapter_uses_openai_compatible_contract(monkeypatch):
    """Adapter 应正确传递鉴权、模型参数并读取标准 Usage。"""

    def handler(url: str, headers: dict, payload: dict) -> httpx.Response:
        assert url == "http://127.0.0.1:18000/v1/chat/completions"
        assert headers == {"Authorization": "Bearer vllm-secret"}
        assert payload == {
            "model": "qwen3-4b",
            "messages": [{"role": "user", "content": "诊断延迟"}],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 512,
        }
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={
            "id": "cmpl-vllm-1",
            "model": "qwen3-4b",
            "choices": [{
                "message": {"role": "assistant", "content": '{"status":"ok"}'},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 21, "completion_tokens": 7},
        })

    monkeypatch.setattr(
        "app.gateway.provider.openai_compatible.httpx.AsyncClient",
        lambda timeout: _FakeAsyncClient(handler, timeout),
    )
    adapter = VllmAdapter("vllm-secret", "http://127.0.0.1:18000/v1/", 180)
    request = ChatCompletionRequest(
        model="vllm/qwen3-4b",
        messages=[{"role": "user", "content": "诊断延迟"}],
        temperature=0,
        max_tokens=512,
    )

    result = asyncio.run(adapter.complete(request, "qwen3-4b"))

    assert result.response_id == "cmpl-vllm-1"
    assert result.content == '{"status":"ok"}'
    assert result.prompt_tokens == 21
    assert result.completion_tokens == 7


def test_vllm_adapter_requires_server_api_key():
    """Gateway 不应在缺少 vLLM 服务端密钥时发送请求。"""
    adapter = VllmAdapter(None, "http://127.0.0.1:18000/v1", 180)
    request = ChatCompletionRequest(
        model="vllm/qwen3-4b",
        messages=[{"role": "user", "content": "hello"}],
    )

    with pytest.raises(ProviderConfigurationError):
        asyncio.run(adapter.complete(request, "qwen3-4b"))


def test_vllm_adapter_removes_leaked_reasoning_prefix():
    """即使服务端解析器未生效，也不能把思考前缀交给 JSON 校验器。"""
    assert _without_thinking(
        '分析过程</think>\n{"type":"final","answer":"完成"}'
    ) == '{"type":"final","answer":"完成"}'
