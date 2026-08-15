"""FastMCP Client 聚合器：本地只读工具 + 第三方 Kubernetes MCP。"""

from __future__ import annotations

from typing import Any

from fastmcp import Client, FastMCP

from app.mcp_clients.kubernetes import KubernetesMCPAdapter
from app.conversation_memory.scope import current_conversation_memory_scope


class ToolExecutionError(Exception):
    """把 MCP transport/tool 异常统一转换为 Agent 可处理的观察结果。"""


class FastMCPToolClient:
    """通过 FastMCP 官方 Client 发现并调用多个标准 MCP Server。

    本项目维护的 FastMCP Server 只包含 Git 与可观测性读取工具；Kubernetes
    语义由第三方 Server 提供。本类是 Client 侧路由，不是自研 MCP 框架。
    """

    def __init__(self, server: FastMCP, kubernetes: KubernetesMCPAdapter | None = None) -> None:
        self.server = server
        self.kubernetes = kubernetes

    async def specifications(self) -> list[dict[str, Any]]:
        """合并标准 tools/list Schema 与审核过的 Kubernetes 语义 Schema。"""
        async with Client(self.server) as client:
            tools = await client.list_tools()
        specifications = [{
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        } for tool in tools if (
            tool.name != "search_conversation_memory"
            or current_conversation_memory_scope() is not None
        )]
        if self.kubernetes:
            specifications.extend(self._kubernetes_specifications())
        return specifications

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """按语义路由工具，并只返回 MCP 的结构化结果。"""
        try:
            if self.kubernetes and name in self.kubernetes.semantic_names():
                return await self.kubernetes.call(name, arguments)
            async with Client(self.server) as client:
                result = await client.call_tool(name, arguments)
            if result.data is not None:
                return result.data
            return {"content": [block.model_dump(mode="json") for block in result.content]}
        except Exception as exc:
            raise ToolExecutionError(f"MCP tool '{name}' failed: {exc}") from exc

    async def close(self) -> None:
        """释放第三方 stdio MCP 进程；本地 in-memory 会话按调用自动关闭。"""
        if self.kubernetes:
            await self.kubernetes.close()

    @staticmethod
    def _kubernetes_specifications() -> list[dict[str, Any]]:
        """只描述工作流允许调用的只读 K8s 语义，绝不暴露写工具。"""
        object_schema = {"type": "object", "additionalProperties": False, "properties": {}}
        named_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        return [
            {"name": "list_namespaces", "description": "通过第三方只读 MCP 列出 Namespace", "input_schema": object_schema},
            {"name": "list_deployments", "description": "通过第三方只读 MCP 列出 Deployment", "input_schema": object_schema},
            {"name": "list_pods", "description": "通过第三方只读 MCP 列出 Pod，可按 label_selector 筛选", "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"label_selector": {"type": "string"}},
            }},
            *[
                {"name": name, "description": f"通过第三方只读 MCP 执行 {name}", "input_schema": named_schema}
                for name in ["get_pod", "get_pod_events", "get_restart_count", "get_deployment", "get_container_image"]
            ],
        ]
