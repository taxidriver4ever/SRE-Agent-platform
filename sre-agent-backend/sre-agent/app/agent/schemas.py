"""Agent 内部决策、执行轨迹和最终响应的数据模型。

使用 Pydantic 统一验证模型返回值与 HTTP 输出，避免模型产生的任意 JSON
未经检查就进入工具执行层。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AgentDecision(BaseModel):
    """模型在一轮推理后给出的结构化决策。

    ``tool`` 决策要求提供工具名和参数；``final`` 决策要求提供最终回答。
    这两个形态共用一个模型，是为了让文本 JSON 协议保持简单。
    """

    type: Literal["tool", "final"] = Field(description="本轮是调用工具还是结束任务")
    tool: str | None = Field(default=None, description="type=tool 时要调用的注册工具名")
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="传递给工具的关键字参数对象",
    )
    answer: str | None = Field(default=None, description="type=final 时返回给用户的答案")

    @model_validator(mode="after")
    def validate_decision(self) -> "AgentDecision":
        """校验由 ``type`` 决定的跨字段约束。

        单个字段的类型校验无法表达“选择工具时必须有工具名”这类条件，
        所以在所有字段完成解析后进行组合校验。
        """
        if self.type == "tool" and not self.tool:
            raise ValueError("tool decision requires tool")
        if self.type == "final" and self.answer is None:
            raise ValueError("final decision requires answer")
        return self


class AgentStep(BaseModel):
    """一次真实工具执行的可观测轨迹。"""

    iteration: int = Field(description="发生工具调用的 Agent 轮次，从 1 开始")
    tool: str = Field(description="实际调用的工具名")
    tool_input: dict[str, Any] = Field(description="模型为工具生成的输入参数")
    observation: Any = Field(description="工具返回值或规范化后的错误对象")
    success: bool = Field(default=True, description="工具本次执行是否成功")


class AgentResult(BaseModel):
    """Agent 对外返回的最终结果及本次运行元数据。"""

    answer: str = Field(description="模型在 final 决策中给出的最终回答")
    steps: list[AgentStep] = Field(description="按发生顺序记录的工具调用轨迹")
    model: str | None = Field(default=None, description="最后一轮实际使用的模型")
    provider: str | None = Field(default=None, description="最后一轮实际使用的厂商")
    prompt_tokens: int = Field(default=0, ge=0, description="所有轮次输入 Token 总数")
    completion_tokens: int = Field(default=0, ge=0, description="所有轮次输出 Token 总数")
