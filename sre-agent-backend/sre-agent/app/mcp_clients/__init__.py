"""Agent 侧 MCP Client 适配层。

这个包只负责连接和调用 MCP Server，不注册任何工具。具体工具仍由独立的
FastMCP Server 或第三方 Kubernetes MCP Server 提供，避免把 Client 和 Tool
实现混放在同一个目录中。
"""

from app.mcp_clients.client import FastMCPToolClient, ToolExecutionError
from app.mcp_clients.kubernetes import KubernetesMCPAdapter

__all__ = ["FastMCPToolClient", "KubernetesMCPAdapter", "ToolExecutionError"]
