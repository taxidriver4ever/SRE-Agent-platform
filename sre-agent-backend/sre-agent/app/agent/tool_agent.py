"""支持多轮工具调用的轻量 JSON-ReAct Agent。

一次运行由“LLM 决策 -> 可选工具执行 -> 观察结果回填”循环组成。该模块只
负责流程编排，不了解网关 HTTP 协议，也不包含任何具体工具实现。
"""

import json

from app.agent.prompt import build_system_prompt
from app.agent.schemas import AgentDecision, AgentResult, AgentStep
from app.llm import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError,
    schema_retry_message,
    template_refill_message,
    validate_structured_output,
)
from app.mcp_clients import FastMCPToolClient, ToolExecutionError


class AgentMaxIterationsError(Exception):
    """Agent 未能在限定轮数内给出最终答案。"""


class ToolAgent:
    """协调 LLM 和工具注册中心完成一个用户任务。

    依赖通过构造函数注入，使生产环境可使用 ``GatewayLLM``，测试中则可使用
    确定性的 Stub LLM；同理，工具集合也能按部署场景自由组合。
    """

    def __init__(self, llm: LLM, tools: FastMCPToolClient, max_iterations: int = 8) -> None:
        """初始化 Agent。

        Args:
            llm: 实现 ``LLM`` 协议的异步模型客户端。
            tools: 本 Agent 被允许调用的工具注册中心。
            max_iterations: 单次任务最大模型轮数，防止异常模型无限循环。

        Raises:
            ValueError: ``max_iterations`` 小于 1 时抛出。
        """
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations

    async def run(self, query: str) -> AgentResult:
        """运行一次 Agent 任务，直到模型给出最终回答或达到轮数上限。

        Args:
            query: 原始用户问题。

        Returns:
            包含最终答案、工具轨迹和累计 Token 用量的 ``AgentResult``。

        Raises:
            GatewayError: LLM 请求失败时由具体 LLM 实现抛出。
            AgentMaxIterationsError: 达到最大轮数仍未产生 final 决策。
        """
        # 首轮只放入稳定的系统协议和用户问题。工具说明由注册中心动态生成，
        # 因此新增工具后无需手动同步提示词中的工具列表。
        messages = [
            LLMMessage("system", build_system_prompt(await self.tools.specifications())),
            LLMMessage("user", query),
        ]
        # ``steps`` 只记录真正发生的工具调用；纯推理轮和协议修复轮不算工具轨迹。
        steps: list[AgentStep] = []

        # 网关逐轮返回用量。这里做运行级累计，便于上层统计一次 Agent 任务的
        # 总成本，而不是只看到最后一次 LLM 请求的消耗。
        total_prompt_tokens = 0
        total_completion_tokens = 0
        last_model: str | None = None
        last_provider: str | None = None
        protocol_failures = 0
        first_invalid_output = ""
        awaiting_template_refill = False

        for iteration in range(1, self.max_iterations + 1):
            # 每一轮都发送完整上下文，因为当前网关提供的是无状态 Chat API。
            response = await self.llm.complete(messages)
            last_model, last_provider = response.model, response.provider
            total_prompt_tokens += response.prompt_tokens
            total_completion_tokens += response.completion_tokens
            # 保存模型原始输出，后续轮次才能理解自己刚才做出的动作。Gateway
            # 要求消息内容非空，所以用一个占位 JSON 表示罕见的空模型响应。
            assistant_content = response.content or '{"type":"invalid_empty_output"}'
            messages.append(LLMMessage("assistant", assistant_content))

            try:
                # 模型输出属于不可信输入，必须先经过 JSON 解析和 Pydantic
                # 跨字段校验，校验通过后才允许进入工具执行路径。
                decision = _parse_decision(response.content)
            except StructuredOutputError as exc:
                # 协议错误不立刻终止任务，而是把结构化错误反馈给模型，允许它
                # 在下一轮自我修复。该修复同样受最大轮数限制。
                if awaiting_template_refill:
                    # 模板回填仍不合法时绝不执行工具，也不写入 AgentStep State。
                    raise AgentMaxIterationsError(
                        "structured output remained invalid after template refill"
                    ) from exc
                protocol_failures += 1
                if not first_invalid_output:
                    first_invalid_output = response.content
                if protocol_failures <= 3:
                    messages.append(LLMMessage("user", schema_retry_message(exc)))
                else:
                    awaiting_template_refill = True
                    messages.append(LLMMessage(
                        "user",
                        template_refill_message(
                            {"type": "final", "tool": None, "tool_input": {}, "answer": ""},
                            first_invalid_output,
                        ),
                    ))
                continue

            protocol_failures = 0
            first_invalid_output = ""
            awaiting_template_refill = False

            if decision.type == "final":
                # final 决策意味着任务完成；返回前同时带回完整可观测轨迹与
                # 所有轮次的 Token 汇总，方便 API 调用方审计。
                return AgentResult(
                    answer=decision.answer or "",
                    steps=steps,
                    model=last_model,
                    provider=last_provider,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                )

            # AgentDecision 的模型校验已经确保 tool 决策一定有工具名。assert
            # 既向类型检查器收窄类型，也能防止未来修改 Schema 后静默破坏约束。
            assert decision.tool is not None
            try:
                observation = await self.tools.execute(decision.tool, decision.tool_input)
                success = True
            except ToolExecutionError as exc:
                # 工具失败属于模型可处理的“观察结果”，不是整个 Agent 的系统
                # 异常。将错误回填后，模型可以修正参数或选择其他工具。
                observation = {"error": str(exc)}
                success = False

            # 对外轨迹使用结构化模型保存输入、输出和成功状态，不暴露内部异常栈。
            steps.append(
                AgentStep(
                    iteration=iteration,
                    tool=decision.tool,
                    tool_input=decision.tool_input,
                    observation=observation,
                    success=success,
                )
            )
            # 网关消息 Schema 暂不支持 ``tool`` role，因此工具观察值封装成
            # user 消息。显式的 type 字段可避免模型把它误解为普通用户问题。
            messages.append(
                LLMMessage(
                    "user",
                    json.dumps(
                        {
                            "type": "tool_result",
                            "tool": decision.tool,
                            "success": success,
                            "result": observation,
                        },
                        ensure_ascii=False,
                    ),
                )
            )

        # 循环正常耗尽说明模型始终没有给出合法 final 决策。
        raise AgentMaxIterationsError(
            f"agent did not finish within {self.max_iterations} iterations"
        )


def _parse_decision(content: str) -> AgentDecision:
    """把模型文本解析并验证为 ``AgentDecision``。

    协议要求裸 JSON；公共结构化输出层会有限兼容代码围栏、前缀文字、尾随逗号
    和单引号，再以严格 Schema 作为是否允许执行工具的最终边界。
    """
    return validate_structured_output(content, AgentDecision)
