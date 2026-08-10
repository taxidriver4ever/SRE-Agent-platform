"""Prometheus、Loki、Tempo 与 MySQL 只读诊断工具。"""

from app.mcp_servers.observability.tools import register_observability_tools

__all__ = ["register_observability_tools"]
