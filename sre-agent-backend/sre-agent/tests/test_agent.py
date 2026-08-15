"""ToolAgent 的决策循环、协议修复和轮数保护测试。"""

import asyncio

import pytest
from fastmcp import FastMCP

from app.agent import AgentMaxIterationsError, ToolAgent
from app.llm import LLMResponse
from app.mcp_clients import FastMCPToolClient


class StubLLM:
    """按预设顺序返回文本的确定性 LLM，测试不访问真实网关。"""

    def __init__(self, responses: list[str]) -> None:
        """保存响应迭代器，并准备记录每轮收到的消息快照。"""
        self.responses = iter(responses)
        self.messages = []

    async def complete(self, messages):
        """模拟一次 LLM 调用，同时保留上下文以便断言观察值已回填。"""
        # copy 防止 Agent 后续 append 修改此前记录的消息列表。
        self.messages.append(messages.copy())
        return LLMResponse(next(self.responses), "stub-model", "stub", 2, 1)


def test_agent_calls_tool_then_returns_final_answer():
    """Agent 应先执行测试专用 Tool，再基于观察结果输出最终答案。"""
    server = FastMCP("agent-tool-test")

    @server.tool(name="echo_number")
    async def echo_number(value: int) -> dict[str, int]:
        """测试专用工具；生产代码不再为了凑框架保留 calculator/builtin。"""
        return {"result": value}

    llm = StubLLM([
        '{"type":"tool","tool":"echo_number","tool_input":{"value":60}}',
        '{"type":"final","answer":"结果是 60"}',
    ])
    agent = ToolAgent(llm, FastMCPToolClient(server), max_iterations=3)

    result = asyncio.run(agent.run("帮我计算"))

    assert result.answer == "结果是 60"
    assert result.steps[0].observation == {"result": 60}
    assert result.prompt_tokens == 4
    assert '"type": "tool_result"' in llm.messages[1][-1].content


def test_agent_can_recover_from_invalid_model_output():
    """模型首轮返回非法 JSON 时，Agent 应反馈协议错误并允许自我修复。"""
    llm = StubLLM(["not-json", '{"type":"final","answer":"已修正"}'])
    tools = FastMCPToolClient(FastMCP("agent-empty-test"))
    result = asyncio.run(ToolAgent(llm, tools, max_iterations=2).run("test"))
    assert result.answer == "已修正"
    assert "protocol_error" in llm.messages[1][-1].content


def test_agent_repairs_common_json_format_without_model_retry():
    """尾随逗号属于纯格式问题，应先本地 JSON Repair 再做 Schema 校验。"""
    llm = StubLLM(['模型结果：{"type":"final","answer":"已修复",}'])
    tools = FastMCPToolClient(FastMCP("agent-json-repair-test"))

    result = asyncio.run(ToolAgent(llm, tools, max_iterations=1).run("test"))

    assert result.answer == "已修复"
    assert len(llm.messages) == 1


def test_agent_uses_template_after_three_model_retries():
    """初次输出加三次定向重试均失败后，应使用预设模板进行最后回填。"""
    llm = StubLLM([
        "bad-initial",
        "bad-retry-1",
        "bad-retry-2",
        "bad-retry-3",
        '{"type":"final","tool":null,"tool_input":{},"answer":"模板已回填"}',
    ])
    tools = FastMCPToolClient(FastMCP("agent-template-refill-test"))

    result = asyncio.run(ToolAgent(llm, tools, max_iterations=5).run("test"))

    assert result.answer == "模板已回填"
    assert "structured_output_template_refill" in llm.messages[4][-1].content


def test_agent_stops_at_iteration_limit():
    """模型持续违反协议时，Agent 必须在配置轮数处停止。"""
    llm = StubLLM(["bad"])
    with pytest.raises(AgentMaxIterationsError):
        tools = FastMCPToolClient(FastMCP("agent-limit-test"))
        asyncio.run(ToolAgent(llm, tools, max_iterations=1).run("test"))
