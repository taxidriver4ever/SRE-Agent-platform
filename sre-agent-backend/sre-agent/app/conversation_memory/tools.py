"""大模型唯一可见的 Conversation Memory 只读 MCP 工具。"""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.conversation_memory.repository import ConversationMemoryRepository
from app.conversation_memory.scope import current_conversation_memory_scope


def register_conversation_memory_tools(
    mcp: FastMCP,
    repository: ConversationMemoryRepository,
) -> None:
    """注册固定表查询；不接受 user_id、conversation_id、表名或 SQL。"""

    @mcp.tool(name="search_conversation_memory")
    async def search_conversation_memory(
        query: str,
        item_types: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        """只检索当前认证用户、当前会话的压缩记忆；最多返回20条。"""
        scope = current_conversation_memory_scope()
        if scope is None:
            raise ToolError("conversation memory scope is not available")
        return {
            "items": repository.search(
                scope.user_id,
                scope.conversation_id,
                query,
                item_types,
                limit,
            )
        }
