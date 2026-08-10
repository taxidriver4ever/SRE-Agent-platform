"""Gateway Chat Completion 请求 Schema。"""

from pydantic import BaseModel, Field, field_validator

from app.gateway.schema.message_schema import ChatMessage


class ChatCompletionRequest(BaseModel):
    """客户端发给 Gateway 的统一非流式 Chat 请求。"""

    model: str = Field(
        min_length=1,
        max_length=200,
        description="目标模型名称；可使用 provider/model 格式，例如 openai/gpt-4o-mini",
        examples=["openai/gpt-4o-mini"],
    )
    messages: list[ChatMessage] = Field(
        min_length=1,
        description="按对话顺序排列的消息列表，至少需要一条消息",
    )
    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        description="生成随机性参数，范围为 0 到 2；为空时使用 Provider 默认值",
    )
    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="允许模型生成的最大 Token 数；为空时使用 Provider 默认值",
    )
    stream: bool = Field(
        default=False,
        description="是否使用流式响应；当前版本只支持 false",
    )

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        """清除模型名称首尾空格，并拒绝纯空白名称。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("model cannot be blank")
        return normalized

    @field_validator("stream")
    @classmethod
    def reject_streaming(cls, value: bool) -> bool:
        """当前最小实现只支持一次性返回，明确拒绝流式请求。"""
        if value:
            raise ValueError("streaming is not supported yet")
        return value

