"""为内存 MCP 工具提供请求级用户与 Conversation 边界。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ConversationMemoryScope:
    user_id: str
    conversation_id: str


_scope: ContextVar[ConversationMemoryScope | None] = ContextVar(
    "conversation_memory_scope", default=None
)


@contextmanager
def conversation_memory_scope(user_id: str, conversation_id: str) -> Iterator[None]:
    """绑定当前认证用户和会话；模型参数无法覆盖这两个值。"""
    token = _scope.set(ConversationMemoryScope(user_id, conversation_id))
    try:
        yield
    finally:
        _scope.reset(token)


def current_conversation_memory_scope() -> ConversationMemoryScope | None:
    return _scope.get()
