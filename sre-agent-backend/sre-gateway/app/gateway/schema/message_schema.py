"""Gateway 请求和响应使用的消息 Schema。"""

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """客户端发送的一条、与具体模型厂商无关的对话消息。"""

    role: Literal["system", "user", "assistant"] = Field(
        description="消息角色：system 表示系统指令，user 表示用户消息，assistant 表示历史模型回复"
    )
    content: str = Field(
        min_length=1,
        description="消息文本内容，不能为空；Gateway 会按 Provider 协议进行转换",
    )


class AssistantMessage(BaseModel):
    """Gateway 返回给客户端的模型回复消息。"""

    role: Literal["assistant"] = Field(
        default="assistant",
        description="回复消息角色，固定为 assistant",
    )
    content: str = Field(
        description="Provider 返回并由 Gateway 统一转换后的模型回复文本"
    )

