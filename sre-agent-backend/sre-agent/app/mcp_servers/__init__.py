"""基于 FastMCP 的标准只读 MCP Server。"""

from app.mcp_servers.factory import build_fastmcp_server

__all__ = ["build_fastmcp_server"]
