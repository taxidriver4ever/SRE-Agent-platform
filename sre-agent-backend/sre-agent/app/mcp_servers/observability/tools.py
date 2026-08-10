"""注册到 FastMCP 的真实本地可观测系统只读工具。"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pymysql
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.mcp_servers.common import bounded

_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|call|load|outfile|dumpfile|set)\b",
    re.IGNORECASE,
)


class HttpObservabilityBackend:
    """FastMCP 工具背后的 Prometheus、Loki、Tempo 只读执行器。"""

    def __init__(
        self,
        name: str,
        endpoints: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> None:
        self.name = name
        self.endpoints = endpoints
        self.timeout = timeout
        self.output_limit = output_limit

    async def execute(self, arguments: dict[str, Any]) -> Any:
        """根据工具名选择固定 GET API；参数会由 httpx 正确 URL 编码。"""
        minutes = min(max(int(arguments.get("time_range_minutes", 30)), 1), 1440)
        limit = min(max(int(arguments.get("limit", 20)), 1), 100)
        service = str(arguments.get("service") or "")
        if service and not _SAFE_LABEL.fullmatch(service):
            raise ToolError("service 含有不允许的字符")

        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                if self.name == "query_metrics":
                    query = str(arguments.get("query") or "").strip()
                    if not query or len(query) > 2000:
                        raise ToolError("query_metrics 需要 1~2000 字符的 PromQL")
                    response = await client.get(f"{self.endpoints['prometheus']}/api/v1/query", params={"query": query})
                elif self.name == "get_service_health":
                    if not service:
                        raise ToolError("get_service_health 必须提供 service")
                    query = f'up{{service="{service}"}}'
                    response = await client.get(f"{self.endpoints['prometheus']}/api/v1/query", params={"query": query})
                elif self.name == "query_logs":
                    response = await self._query_logs(client, arguments, service, minutes, limit, now)
                elif self.name == "query_trace":
                    response = await self._query_trace(client, arguments, service, limit)
                else:
                    raise ToolError(f"未审核的可观测操作: {self.name}")
                response.raise_for_status()
            except ToolError:
                raise
            except httpx.HTTPError as exc:
                raise ToolError(f"{self.name} 上游查询失败: {exc}") from exc

        body = response.json()
        data = body.get("data", body)
        return bounded({"source": self.name, "time_range_minutes": minutes, "result": data}, self.output_limit)

    async def _query_logs(
        self,
        client: httpx.AsyncClient,
        arguments: dict[str, Any],
        service: str,
        minutes: int,
        limit: int,
        now: datetime,
    ) -> httpx.Response:
        """从结构化过滤条件生成受控 LogQL，而不是接受任意写入端点。"""
        selector = f'{{service_name="{service}"}}' if service else '{namespace="sre-lab"}'
        level = str(arguments.get("level") or "").lower()
        keyword = str(arguments.get("keyword") or "")[:120]
        if level:
            if not _SAFE_LABEL.fullmatch(level):
                raise ToolError("level 含有不允许的字符")
            selector += f' |= "\\\"level\\\":\\\"{level.upper()}\\\""'
        if keyword:
            # 双引号和反斜线转义，确保 keyword 只作为日志正文过滤值。
            escaped = keyword.replace("\\", "\\\\").replace('"', '\\"')
            selector += f' |= "{escaped}"'
        return await client.get(
            f"{self.endpoints['loki']}/loki/api/v1/query_range",
            params={
                "query": selector,
                "start": str(int((now - timedelta(minutes=minutes)).timestamp() * 1_000_000_000)),
                "end": str(int(now.timestamp() * 1_000_000_000)),
                "limit": limit,
                "direction": "backward",
            },
        )

    async def _query_trace(
        self,
        client: httpx.AsyncClient,
        arguments: dict[str, Any],
        service: str,
        limit: int,
    ) -> httpx.Response:
        """优先按 trace_id 精确取 Trace，否则按运行服务名搜索最近 Trace。"""
        trace_id = str(arguments.get("trace_id") or "")
        if trace_id:
            if not re.fullmatch(r"[0-9a-fA-F]{16,32}", trace_id):
                raise ToolError("trace_id 必须是 16~32 位十六进制字符串")
            return await client.get(f"{self.endpoints['tempo']}/api/traces/{trace_id}")
        if not service:
            raise ToolError("query_trace 必须提供 service 或 trace_id")
        return await client.get(
            f"{self.endpoints['tempo']}/api/search",
            params={"tags": f"service.name={service}", "limit": limit},
        )


class MySQLReadBackend:
    """FastMCP 工具背后的 MySQL 只读诊断执行器。"""

    def __init__(self, name: str, database: dict[str, Any], timeout: float, output_limit: int) -> None:
        self.name = name
        self.database = database
        self.timeout = timeout
        self.output_limit = output_limit

    async def execute(self, arguments: dict[str, Any]) -> Any:
        """在线程中执行同步驱动，并由 asyncio timeout 约束总耗时。"""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._query, arguments),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            raise ToolError(f"MySQL 查询超过 {self.timeout:.0f} 秒") from exc
        except pymysql.MySQLError as exc:
            raise ToolError(f"MySQL 只读查询失败: {exc}") from exc
        return bounded(result, self.output_limit)

    def _query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """打开短连接、执行单条只读语句并立刻关闭，避免长期占用实验连接池。"""
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        minutes = min(max(int(arguments.get("time_range_minutes", 30)), 1), 1440)
        if self.name == "query_slow_queries":
            sql = (
                "SELECT start_time, query_time, lock_time, rows_sent, rows_examined, "
                "LEFT(sql_text, 500) AS sql_text FROM mysql.slow_log "
                "WHERE start_time >= NOW() - INTERVAL %s MINUTE ORDER BY start_time DESC LIMIT %s"
            )
            params: tuple[Any, ...] = (minutes, limit)
        elif self.name == "query_sql_digest":
            sql = (
                "SELECT digest_text, count_star, ROUND(sum_timer_wait/1000000000000,3) AS total_seconds, "
                "sum_rows_examined FROM performance_schema.events_statements_summary_by_digest "
                "WHERE schema_name=%s ORDER BY sum_timer_wait DESC LIMIT %s"
            )
            params = (self.database["database"], limit)
        elif self.name == "explain_sql":
            requested = str(arguments.get("sql") or "").strip().rstrip(";")
            self._validate_select(requested)
            sql = f"EXPLAIN {requested}"
            params = ()
        else:
            raise ToolError(f"未审核的 MySQL 操作: {self.name}")

        connection = pymysql.connect(
            host=self.database["host"], port=self.database["port"], user=self.database["user"],
            password=self.database["password"], database=self.database["database"],
            connect_timeout=max(1, int(self.timeout)), read_timeout=max(1, int(self.timeout)),
            cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                # PyMySQL 传入空元组时仍会执行 Python `%` 格式化，LIKE '%x%' 会因此报错。
                # 无绑定参数的已验证 EXPLAIN 直接执行 SQL；其他查询继续使用参数绑定。
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                rows = cursor.fetchall()
                return {"query_type": self.name, "row_count": len(rows), "rows": rows}
        finally:
            connection.close()

    @staticmethod
    def _validate_select(sql: str) -> None:
        """拒绝多语句、注释和任何写关键词，只放行单条 SELECT。"""
        normalized = sql.strip()
        if not normalized.lower().startswith("select "):
            raise ToolError("explain_sql 只接受 SELECT 语句")
        if ";" in normalized or "--" in normalized or "/*" in normalized or _FORBIDDEN_SQL.search(normalized):
            raise ToolError("SQL 包含多语句、注释或写操作关键词，已拒绝")
        if len(normalized) > 4000:
            raise ToolError("SQL 超过 4000 字符")


def register_observability_tools(mcp: FastMCP, settings: Any) -> None:
    """注册 Prometheus、Loki、Tempo 和 MySQL FastMCP 只读工具。"""
    endpoints = {
        "prometheus": settings.prometheus_base_url,
        "loki": settings.loki_base_url,
        "tempo": settings.tempo_base_url,
    }
    db = {
        "host": settings.mysql_host, "port": settings.mysql_port, "user": settings.mysql_user,
        "password": settings.mysql_password, "database": settings.mysql_database,
    }
    http_names = ["query_metrics", "query_logs", "query_trace", "get_service_health"]
    mysql_names = ["query_slow_queries", "query_sql_digest", "explain_sql"]
    for operation in http_names:
        handler = HttpObservabilityBackend(
            operation, endpoints, settings.tool_timeout_seconds, settings.tool_output_limit,
        )

        def create_http_tool(current_handler: HttpObservabilityBackend):
            async def observability_read(
                query: str | None = None,
                service: str | None = None,
                level: str | None = None,
                keyword: str | None = None,
                time_range_minutes: int = 30,
                limit: int = 20,
                trace_id: str | None = None,
            ) -> dict[str, Any]:
                """执行受约束的 Prometheus、Loki 或 Tempo 查询。"""
                return await current_handler.execute({
                    "query": query,
                    "service": service,
                    "level": level,
                    "keyword": keyword,
                    "time_range_minutes": time_range_minutes,
                    "limit": limit,
                    "trace_id": trace_id,
                })
            return observability_read

        mcp.tool(
            name=operation,
            description=f"只读可观测性查询 {operation}，返回结构化且受大小限制的结果",
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            tags={"observability", "readonly"},
        )(create_http_tool(handler))

    for operation in mysql_names:
        handler = MySQLReadBackend(
            operation, db, settings.tool_timeout_seconds, settings.tool_output_limit,
        )

        def create_mysql_tool(current_handler: MySQLReadBackend):
            async def mysql_read(
                sql: str | None = None,
                limit: int = 10,
                time_range_minutes: int = 30,
            ) -> dict[str, Any]:
                """使用专用只读账号查询 MySQL 诊断证据。"""
                return await current_handler.execute({
                    "sql": sql,
                    "limit": limit,
                    "time_range_minutes": time_range_minutes,
                })
            return mysql_read

        mcp.tool(
            name=operation,
            description=f"MySQL 只读诊断 {operation}；仅允许 SELECT 或 EXPLAIN SELECT",
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            tags={"mysql", "readonly"},
        )(create_mysql_tool(handler))
