"""Conversation HTTP API 的请求和响应模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    """创建空会话时使用的可选标题。"""

    title: str = Field(default="新诊断", min_length=1, max_length=120)


class ConversationSummary(BaseModel):
    """会话列表只返回摘要，避免进入页面时加载所有历史大报告。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class ConversationMessage(BaseModel):
    """一条持久化消息；assistant content 可以是完整诊断报告 JSON。"""

    id: str
    role: Literal["user", "assistant"]
    content: Any
    created_at: str


class ConversationAttachment(BaseModel):
    """持久化附件只暴露 MinIO Key；下载时再申请短时效签名。"""

    oss_key: str
    created_at: str


class ConversationDetail(ConversationSummary):
    """选择历史会话时返回按时间排序的全部消息。"""

    messages: list[ConversationMessage]
    attachments: list[ConversationAttachment] = Field(default_factory=list)
