"""Request equivalence, cancellation and metering at the framework boundary."""

import asyncio
import json

import httpx
import pytest

from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import GatewayLLM, GatewayRequestError
from app.llm.langchain import LangChainLLM
from app.llm.prompts import prompt_messages
from app.agent.prompt import build_system_prompt


def test_framework_gateway_request_and_response_are_identical():
    async def run():
        requests = []
        async def handle(request):
            requests.append((request.url.path, request.headers["Authorization"], json.loads(request.content)))
            return httpx.Response(200, json={
                "model": "actual", "provider": "local", "choices": [{"message": {"content": ""}}],
                "usage": {"prompt_tokens": 13, "completion_tokens": 2},
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            legacy = GatewayLLM("http://gateway", "test", "route", client=client, max_tokens=384)
            messages = [LLMMessage("system", ' JSON {"k": "值"}\n'), LLMMessage("user", "{not_a_variable}"),
                        LLMMessage("assistant", "{}"), LLMMessage("user", '{"type":"tool_result"}')]
            expected = await legacy.complete(messages)
            actual = await LangChainLLM(legacy).complete(messages)
            assert actual == expected
            assert requests[0] == requests[1]
            assert len(requests) == 2
    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500])
def test_framework_does_not_retry_gateway_errors(status):
    async def run():
        calls = 0
        async def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(status, json={"detail": "unavailable"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = LangChainLLM(GatewayLLM("http://gateway", "test", "route", client=client))
            with pytest.raises(GatewayRequestError, match=str(status)):
                await llm.complete([LLMMessage("user", "hello")])
        assert calls == 1
    asyncio.run(run())


def test_framework_cancellation_reaches_transport():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Transport:
            async def complete(self, messages):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        task = asyncio.create_task(LangChainLLM(Transport()).complete([LLMMessage("user", "hello")]))
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
    prompt = build_system_prompt([{"name": "x", "input_schema": {"type": "object"}}])
    assert '{"type":"tool","tool":"工具名","tool_input":{...}}' in prompt
    assert json.dumps([{"name": "x", "input_schema": {"type": "object"}}], ensure_ascii=False, indent=2) in prompt


def test_framework_has_no_cache_and_closes_transport_once():
    async def run():
        class Transport:
            calls = 0
            closed = 0
            async def complete(self, messages):
                self.calls += 1
                return LLMResponse(str(self.calls), "model")
            async def close(self):
                self.closed += 1
        transport = Transport()
        llm = LangChainLLM(transport)
        for expected in ["1", "2"]:
            assert (await llm.complete([LLMMessage("user", "same")])).content == expected
        await llm.close()
        assert transport.closed == 1
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["legacy", "langchain"])
def test_backend_switch_wires_both_llm_and_mcp_without_changing_health(monkeypatch, backend):
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv("SRE_AGENT_BACKEND", backend)
    with TestClient(create_app()) as client:
        assert isinstance(client.app.state.llm, LangChainLLM) == (backend == "langchain")
        assert client.app.state.tools.backend == backend
        assert client.app.state.tools.kubernetes.backend == backend
        assert client.get("/health").json() == {"status": "ok"}
