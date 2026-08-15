"""集中组装标准 FastMCP Server。"""

from typing import Any

from fastmcp import FastMCP

from app.mcp_servers.git import register_git_tools
from app.mcp_servers.observability import register_observability_tools
from app.conversation_memory import ConversationMemoryRepository, register_conversation_memory_tools
from app.code_state import CodeStateRepository, register_code_state_tools
from app.repositories import RepositoryRegistry


def build_fastmcp_server(
    settings: Any,
    registry: RepositoryRegistry | None = None,
    memory_repository: ConversationMemoryRepository | None = None,
    code_state_repository: CodeStateRepository | None = None,
) -> FastMCP:
    """创建项目自有只读工具集；Kubernetes 由独立第三方 MCP Server 提供。"""
    mcp = FastMCP(
        name="SRE Agent Read-Only Tools",
        instructions="只允许诊断读取；禁止修改 Kubernetes、Git、数据库和外部系统。",
    )
    register_observability_tools(mcp, settings)
    register_git_tools(
        mcp,
        settings.repository_path,
        settings.service_catalog_path,
        settings.tool_timeout_seconds,
        settings.tool_output_limit,
        registry=registry,
    )
    if memory_repository is not None:
        register_conversation_memory_tools(mcp, memory_repository)
    if code_state_repository is not None:
        register_code_state_tools(mcp, code_state_repository)
    return mcp
