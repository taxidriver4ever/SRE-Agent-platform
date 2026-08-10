"""Gateway Chat Completion 响应 Schema。"""

from typing import Literal

from pydantic import BaseModel, Field

from app.gateway.schema.message_schema import AssistantMessage
from app.gateway.schema.usage_schema import ChatUsage


class ChatChoice(BaseModel):
    """统一响应中的一个模型生成结果。"""

    index: int = Field(
        default=0,
        ge=0,
        description="结果在 choices 数组中的位置；当前非流式实现固定返回第 0 项",
    )
    message: AssistantMessage = Field(
        description="模型生成的 Assistant 消息"
    )
    finish_reason: str | None = Field(
        default=None,
        description="模型停止生成的原因，例如 stop、length 或 content_filter",
    )


class ChatCompletionResponse(BaseModel):
    """Gateway 返回客户端的统一 Chat Completion 响应。"""

    id: str = Field(description="Provider 返回或 Gateway 生成的本次响应唯一 ID")
    object: Literal["chat.completion"] = Field(
        default="chat.completion",
        description="响应对象类型，固定为 chat.completion",
    )
    created: int = Field(
        description="Gateway 创建响应时的 Unix 时间戳，单位为秒"
    )
    model: str = Field(description="Provider 实际调用并返回结果的模型名称")
    provider: str = Field(
        description="实际处理请求的模型厂商，例如 openai、claude、deepseek 或 ollama"
    )
    choices: list[ChatChoice] = Field(
        description="模型生成结果列表；当前版本返回一个结果"
    )
    usage: ChatUsage = Field(description="本次模型调用的统一 Token 用量统计")
    latency_ms: int = Field(
        ge=0,
        description="Gateway 调用 Provider 并获得结果所花费的时间，单位为毫秒",
    )

