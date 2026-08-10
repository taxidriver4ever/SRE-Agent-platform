"""跨 Provider 的统一 Token Usage Schema。"""

from pydantic import BaseModel, Field


class ChatUsage(BaseModel):
    """一次模型调用消耗的输入、输出及总 Token 数。"""

    prompt_tokens: int = Field(
        default=0,
        ge=0,
        description="Provider 统计的请求输入 Token 数量",
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        description="Provider 统计的模型输出 Token 数量",
    )
    total_tokens: int = Field(
        default=0,
        ge=0,
        description="本次调用总 Token 数，等于输入 Token 数加输出 Token 数",
    )

