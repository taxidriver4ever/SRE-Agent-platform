"""GatewayLLM 的请求协议、响应转换和配置校验测试。"""

import asyncio
import json

import httpx
import pytest

from app.llm import GatewayConfigurationError, GatewayLLM, LLMMessage


def test_gateway_llm_uses_expected_endpoint_and_token():
    """客户端应使用网关路径和 Bearer Token，并正确解析统一响应。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """MockTransport 处理器：验证出站请求并构造网关响应。"""
        assert request.url.path == "/v1/gateway/chat/completions"
        assert request.headers["Authorization"] == "Bearer gw_sk_test"
        payload = json.loads(request.content)
        assert payload["model"] == "openai/test-model"
        return httpx.Response(200, json={
            "model": "test-model",
            "provider": "openai",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })

    async def run():
        """在单一事件循环内创建、使用并关闭异步客户端。"""
        # MockTransport 完全拦截 HTTP，因此测试不会依赖已启动的网关服务。
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        llm = GatewayLLM("http://gateway", "gw_sk_test", "openai/test-model", client=client)
        response = await llm.complete([LLMMessage("user", "hello")])
        await client.aclose()
        return response

    response = asyncio.run(run())

    assert response.content == "ok"
    assert response.provider == "openai"
    assert response.prompt_tokens == 3


def test_gateway_llm_requires_api_key():
    """真正发起请求前必须检查 Gateway API Key。"""

    async def run():
        """执行缺失密钥场景并确保自建连接池最终关闭。"""
        llm = GatewayLLM("http://gateway", None, "openai/test-model")
        with pytest.raises(GatewayConfigurationError):
            await llm.complete([LLMMessage("user", "hello")])
        await llm.close()

    asyncio.run(run())
