"""模型只读的固定 Code State 表导航工具。"""

from fastmcp import FastMCP

from app.code_state.repository import CodeStateRepository


def register_code_state_tools(mcp: FastMCP, repository: CodeStateRepository) -> None:
    @mcp.tool(name="search_code_state")
    async def search_code_state(
        repository_name: str,
        query: str,
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> dict[str, object]:
        """从固定 code_state_components 表查找模块、symbol、文件和 Git Reference。"""
        return {"components": repository.search(repository_name, query, kinds, limit)}
