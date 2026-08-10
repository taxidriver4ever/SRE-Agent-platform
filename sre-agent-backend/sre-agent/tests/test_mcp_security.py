"""MCP 最小权限、输入校验与输出上限的安全回归测试。"""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.core.config import get_settings
from app.mcp_clients import FastMCPToolClient, KubernetesMCPAdapter
from app.mcp_servers import build_fastmcp_server
from app.mcp_servers.common import bounded
from app.mcp_servers.git.tools import GitReadBackend
from app.mcp_servers.observability.tools import MySQLReadBackend
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.models import DiagnosisState


def test_mcp_factory_exposes_required_read_tools_only():
    """工具清单必须覆盖任务要求，同时不能出现任何集群写操作。"""
    async def list_names() -> set[str]:
        settings = get_settings()
        client = FastMCPToolClient(
            build_fastmcp_server(settings), KubernetesMCPAdapter(settings.kubernetes_namespace)
        )
        return {tool["name"] for tool in await client.specifications()}

    names = asyncio.run(list_names())
    required = {
        "list_namespaces", "list_deployments", "list_pods", "get_pod", "get_pod_events",
        "get_container_image", "query_metrics", "query_logs", "query_trace",
        "query_slow_queries", "query_sql_digest", "explain_sql", "get_commit_diff",
        "read_file_at_commit", "search_code", "list_changed_files",
    }
    assert required.issubset(names)
    assert names.isdisjoint({"delete_pod", "restart", "scale", "apply", "patch"})


def test_kubernetes_adapter_exposes_no_write_semantics():
    """项目 Client 只能路由第三方 Server 的只读语义，不能暴露写入或 exec。"""
    names = KubernetesMCPAdapter.semantic_names()
    assert {"list_pods", "get_pod", "get_pod_events", "get_container_image"}.issubset(names)
    assert names.isdisjoint({"delete_pod", "pods_exec", "resources_create_or_update", "resources_delete"})


def test_extract_trace_id_from_loki_json_log():
    """工作流应使用日志中的真实 trace_id 精确关联 Tempo，而不是猜测链路。"""
    payload = {
        "data": {
            "source": "query_logs",
            "result": {
                "resultType": "streams",
                "result": [
                    {
                        "values": [
                            [
                                "1720000000000000000",
                                '{"service":"order-service","message":"mode=slow_sql",'
                                '"trace_id":"f20b2ccf2b99608c2f8585ad9035866a"}',
                            ]
                        ]
                    }
                ],
            },
        }
    }

    assert DiagnosisWorkflow._extract_trace_id(payload) == "f20b2ccf2b99608c2f8585ad9035866a"


def test_mixed_pod_versions_select_the_minority_runtime():
    """混合发布时应把少数版本 Pod 选为调查目标，而不是读取 Deployment 平均状态。"""
    good = "a" * 40
    bad = "b" * 40
    payload = {"data": {"items": [
        {"metadata": {"name": "order-good-a"}, "spec": {"containers": [{"image": f"sre-lab/order-service:{good}"}]}},
        {"metadata": {"name": "order-good-b"}, "spec": {"containers": [{"image": f"sre-lab/order-service:{good}"}]}},
        {"metadata": {"name": "order-bad-c"}, "spec": {"containers": [{"image": f"sre-lab/order-service:{bad}"}]}},
    ]}}
    state = DiagnosisState(query="为什么订单有时候特别慢", service="order-service")

    workflow = object.__new__(DiagnosisWorkflow)
    workflow.repository_registry = None
    workflow._extract_pod_runtime(state, payload)

    assert state.mixed_versions is True
    assert state.pod_name == "order-bad-c"
    assert state.runtime_commit == bad


def test_git_tool_rejects_repository_outside_catalog():
    """repository 参数只能从 Catalog 白名单选择，不能成为任意文件系统路径。"""
    settings = get_settings()
    allowed = {"order-service": Path(settings.repository_path) / "order-service"}
    tool = GitReadBackend("read_file", settings.repository_path, timeout=1, output_limit=1000, repositories=allowed)
    with pytest.raises(ToolError, match="未知或未授权"):
        asyncio.run(tool.execute({"repository": "../../Windows", "path": "win.ini"}))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET status='PAID'",
        "SELECT * FROM orders; DELETE FROM orders",
        "SELECT * FROM orders /* bypass */",
        "SELECT * FROM orders INTO OUTFILE 'x'",
    ],
)
def test_mysql_explain_rejects_writes_multistatements_and_comments(sql: str):
    """即使数据库账号配置错误，代码层仍必须先拒绝危险 SQL。"""
    with pytest.raises(ToolError):
        MySQLReadBackend._validate_select(sql)


def test_mysql_explain_accepts_one_plain_select():
    """合法的单条 SELECT 可以进入 EXPLAIN 流程。"""
    MySQLReadBackend._validate_select("SELECT COUNT(*) FROM orders WHERE id = 1")


def test_mysql_explain_accepts_literal_percent_wildcard():
    """合法 LIKE 百分号应由无参数 execute 路径原样交给 MySQL，而不是 Python 格式化。"""
    MySQLReadBackend._validate_select("SELECT COUNT(*) FROM orders WHERE customer_email LIKE '%slow.example.com%'")


def test_git_path_cannot_escape_repository():
    """源码读取路径 resolve 后必须仍位于配置的仓库根目录。"""
    settings = get_settings()
    repository = Path(settings.repository_path) / "order-service"
    tool = GitReadBackend(
        "read_file",
        settings.repository_path,
        timeout=1,
        output_limit=1000,
        repositories={"order-service": repository},
    )
    with pytest.raises(ToolError, match="不能逃逸"):
        asyncio.run(tool.execute({"repository": "order-service", "path": "..\\outside-secret.txt"}))


def test_bounded_marks_and_truncates_large_results():
    """日志或源码超过上限时必须显式标记截断，而不是悄悄灌入模型。"""
    result = bounded({"logs": "x" * 500}, limit=80)
    assert result["truncated"] is True
    assert len(result["data"]) == 80
    assert result["characters"] > 80
