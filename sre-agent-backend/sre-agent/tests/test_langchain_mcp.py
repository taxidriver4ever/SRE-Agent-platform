"""Official adapter equivalence against a real in-process FastMCP server."""

import asyncio

import pytest
from fastmcp import FastMCP

from app.mcp_clients.client import FastMCPToolClient, ToolExecutionError
from app.security.policy import ToolPolicyError


def test_official_tools_preserve_specs_typed_results_and_errors():
    async def run():
        server = FastMCP("contract")
        @server.tool
        def mapping() -> dict:
            return {"data": {"中文": [1, 2]}, "truncated": False}
        @server.tool
        def sequence() -> list[int]:
            return [1, 2]
        @server.tool
        def scalar() -> int:
            return 7
        @server.tool
        def text() -> str:
            return "ordinary text"
        @server.tool
        def failure() -> dict:
            raise ValueError("fixture failed")
        old = FastMCPToolClient(server)
        new = FastMCPToolClient(server, backend="langchain")
        assert await old.specifications() == await new.specifications()
        for name in ["mapping", "sequence", "scalar", "text"]:
            assert await old.execute(name, {}) == await new.execute(name, {})
        for name in ["failure", "missing"]:
            errors = []
            for client in [old, new]:
                with pytest.raises(ToolExecutionError) as exc:
                    await client.execute(name, {})
                errors.append(str(exc.value))
            assert errors[0] == errors[1]
    asyncio.run(run())


def test_official_adapter_preserves_context_concurrency_and_audit():
    from app.security import task_security_scope, current_task_scope
    async def run():
        server = FastMCP("context")
        calls, audits = [], []
        @server.tool
        async def observe(value: int) -> dict:
            await asyncio.sleep(0)
            scope = current_task_scope()
            calls.append(scope.user_id)
            return {"user": scope.user_id, "value": value}
        class Audit:
            def record(self, scope, name, args, status, duration, error):
                audits.append((scope.user_id, status))
        client = FastMCPToolClient(server, backend="langchain", audit_repository=Audit())
        async def invoke(user, value):
            with task_security_scope(user, "sre-lab", user):
                return await client.execute("observe", {"value": value})
        results = await asyncio.gather(invoke("a", 1), invoke("b", 2))
        assert results == [{"user": "a", "value": 1}, {"user": "b", "value": 2}]
        assert sorted(calls) == ["a", "b"]
        assert sorted(audits) == [("a", "success"), ("b", "success")]
    asyncio.run(run())


def test_policy_denial_precedes_mcp_and_is_audited_once():
    async def run():
        server = FastMCP("denied")
        calls, audits = [], []
        @server.tool
        def secret() -> dict:
            calls.append(1)
            return {}
        class Policy:
            def authorize(self, *args):
                raise ToolPolicyError("denied")
        class Audit:
            def record(self, scope, name, args, status, duration, error):
                audits.append(status)
        client = FastMCPToolClient(server, backend="langchain", policy=Policy(), audit_repository=Audit())
        with pytest.raises(ToolExecutionError, match="Policy denied"):
            await client.execute("secret", {})
        assert calls == []
        assert audits == ["denied"]
    asyncio.run(run())


def test_kubernetes_official_adapter_preserves_yaml_semantics_and_lazy_session():
    from fastmcp import Client
    from app.mcp_clients.kubernetes import KubernetesMCPAdapter
    async def run():
        server = FastMCP("kubernetes fixture")
        @server.tool
        def pods_get(namespace: str, name: str) -> str:
            assert namespace == "sre-lab"
            return "metadata:\n  name: pod-a\nstatus:\n  containerStatuses:\n    - restartCount: 3\n"
        adapters = [KubernetesMCPAdapter("sre-lab", backend=backend) for backend in ["legacy", "langchain"]]
        try:
            results = []
            for adapter in adapters:
                adapter._client = Client(server)
                assert not adapter._entered
                results.append(await asyncio.gather(
                    adapter.call("get_pod", {"name": "pod-a"}),
                    adapter.call("get_restart_count", {"name": "pod-a"}),
                ))
                assert adapter._entered
            assert results[0] == results[1]
            assert results[1][1]["data"]["restart_count"] == 3
        finally:
            for adapter in adapters:
                await adapter.close()
                assert not adapter._entered
    asyncio.run(run())


def test_agent_observation_and_framework_trace_are_correlated(caplog):
    import json
    import logging
    from app.agent import ToolAgent
    from app.agent.observability import agent_call_context
    from app.llm.base import LLMResponse
    from app.llm.langchain import LangChainLLM
    async def run():
        server = FastMCP("agent fixture")
        @server.tool
        def query_metrics(query: str) -> dict:
            return {"data": {"up": 1}}
        messages = []
        class Transport:
            async def complete(self, history):
                messages.append(list(history))
                content = ('{"type":"tool","tool":"query_metrics","tool_input":{"query":"up"}}'
                           if len(messages) == 1 else '{"type":"final","answer":"healthy"}')
                return LLMResponse(content, "fixture", prompt_tokens=2, completion_tokens=1)
        agent = ToolAgent(LangChainLLM(Transport()), FastMCPToolClient(server, backend="langchain"), 2)
        with agent_call_context(correlation_id="run-fixture", step_id="step-fixture", phase="INVESTIGATE"):
            result = await agent.run("health")
        assert result.answer == "healthy"
        assert len(result.steps) == 1
        assert result.prompt_tokens == 4 and result.completion_tokens == 2
        assert json.loads(messages[1][-1].content) == {
            "type": "tool_result", "tool": "query_metrics", "success": True, "result": {"data": {"up": 1}},
        }
        assert messages[1][-1].role == "user"
    caplog.set_level(logging.INFO, logger="app.agent.observability")
    asyncio.run(run())
    assert "run-fixture" in caplog.text and "step-fixture" in caplog.text
    assert "agent.call.started" in caplog.text and "agent.call.finished" in caplog.text


def test_memory_tools_are_request_scoped_without_global_tool_cache():
    from app.conversation_memory.scope import conversation_memory_scope, current_conversation_memory_scope
    async def run():
        server = FastMCP("memory fixture")
        @server.tool
        def search_conversation_memory() -> dict:
            scope = current_conversation_memory_scope()
            if scope is None:
                raise ValueError("missing conversation scope")
            return {"user": scope.user_id, "conversation": scope.conversation_id}
        client = FastMCPToolClient(server, backend="langchain")
        assert await client.specifications() == []
        async def invoke(user, conversation):
            with conversation_memory_scope(user, conversation):
                assert [item["name"] for item in await client.specifications()] == ["search_conversation_memory"]
                return await client.execute("search_conversation_memory", {})
        assert await asyncio.gather(invoke("a", "c1"), invoke("b", "c2")) == [
            {"user": "a", "conversation": "c1"}, {"user": "b", "conversation": "c2"},
        ]
        assert await client.specifications() == []
    asyncio.run(run())
