"""Direct Gateway request, prompt, metering and lifecycle contracts."""

import asyncio
import json

import httpx
import pytest

from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import GatewayLLM, GatewayRequestError
from app.llm.prompts import prompt_messages
from app.agent.prompt import build_system_prompt


def test_gateway_preserves_full_request_and_response():
    async def run():
        requests = []
        async def handle(request):
            requests.append((request.url.path, request.headers["Authorization"], json.loads(request.content)))
            return httpx.Response(200, json={
                "model": "actual", "provider": "local", "choices": [{"message": {"content": ""}}],
                "usage": {"prompt_tokens": 13, "completion_tokens": 2},
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = GatewayLLM("http://gateway", "test", "route", client=client, max_tokens=384)
            messages = [LLMMessage("system", ' JSON {"k": "值"}\n'), LLMMessage("user", "{not_a_variable}"),
                        LLMMessage("assistant", "{}"), LLMMessage("user", '{"type":"tool_result"}')]
            assert await llm.complete(messages) == LLMResponse("", "actual", "local", 13, 2)
            assert requests == [("/v1/gateway/chat/completions", "Bearer test", {
                "model": "route", "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": 0, "max_tokens": 384, "stream": False,
            })]
    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500])
def test_gateway_errors_are_not_retried(status):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(status, json={"detail": "unavailable"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = GatewayLLM("http://gateway", "test", "route", client=client)
            with pytest.raises(GatewayRequestError, match=str(status)):
                await llm.complete([LLMMessage("user", "hello")])
        assert len(calls) == 1
    asyncio.run(run())


def test_gateway_cancellation_reaches_http_transport():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = GatewayLLM("http://gateway", "test", "route", client=client)
            task = asyncio.create_task(llm.complete([LLMMessage("user", "hello")]))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
    asyncio.run(run())


def test_prompt_preserves_json_whitespace_and_roles():
    assert prompt_messages('规则 {"key": 1}\n', "  {system} 中文  ") == [
        LLMMessage("system", '规则 {"key": 1}\n'), LLMMessage("user", "  {system} 中文  "),
    ]
    specs = [{"name": "x", "description": " {tools_json} 中文 ", "input_schema": {"type": "object"}}]
    prompt = build_system_prompt(specs)
    assert '{"type":"tool","tool":"工具名","tool_input":{...}}' in prompt
    assert json.dumps(specs, ensure_ascii=False, indent=2) in prompt


def test_gateway_no_cache_and_client_ownership():
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": str(len(calls))}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = GatewayLLM("http://gateway", "test", "route", client=client)
            for expected in ["1", "2"]:
                assert (await llm.complete([LLMMessage("user", "same")])).content == expected
            await llm.close()
            assert not client.is_closed
        owned = GatewayLLM("http://gateway", "test", "route")
        await owned.close()
        assert owned._client.is_closed
    asyncio.run(run())


def test_application_injects_gateway_and_preserves_health():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.mcp_clients import FastMCPToolClient, KubernetesMCPAdapter
    with TestClient(create_app()) as client:
        assert type(client.app.state.llm) is GatewayLLM
        assert isinstance(client.app.state.tools, FastMCPToolClient)
        assert isinstance(client.app.state.tools.kubernetes, KubernetesMCPAdapter)
        assert client.app.state.agent.llm is client.app.state.llm
        assert client.get("/health").json() == {"status": "ok"}
