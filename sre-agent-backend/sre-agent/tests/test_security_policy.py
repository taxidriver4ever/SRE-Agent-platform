"""项目级 Tool Policy、Audit 与 Docker Sandbox 安全回归测试。"""

import asyncio
from pathlib import Path

import pytest
from fastmcp import FastMCP

from app.mcp_clients import FastMCPToolClient, ToolExecutionError
from app.audit import ToolAuditRepository, initialize_audit_schema
from app.sandbox import DockerSandboxManager, SandboxError
from app.security import ToolPolicy, ToolPolicyError, task_security_scope
from app.security.models import TaskSecurityScope
from tests.mysql_support import mysql_test_database


def build_policy() -> ToolPolicy:
    root = Path(__file__).resolve().parents[3]
    repositories = {
        name: root / "sre-broken-system" / name
        for name in (
            "order-service", "inventory-service", "user-service", "payment-service",
            "notification-service", "recommendation-service",
        )
    }
    return ToolPolicy(
        Path(__file__).resolve().parents[1] / "config" / "tool-policy.yaml",
        repositories,
    )


def test_policy_rejects_unknown_tool_project_and_path_escape() -> None:
    policy = build_policy()
    scope = TaskSecurityScope("u1", "sre-lab", "task-1")
    with pytest.raises(ToolPolicyError, match="not allowed"):
        policy.authorize("shell", {"command": "whoami"}, scope)
    with pytest.raises(ToolPolicyError, match="repository root"):
        policy.authorize(
            "read_file",
            {"repository": "order-service", "path": "../outside.txt"},
            scope,
        )
    with pytest.raises(ToolPolicyError, match="unknown"):
        policy.project("another-project")


def test_policy_rejects_extra_parameters_and_multiline_promql() -> None:
    policy = build_policy()
    scope = TaskSecurityScope("u1", "sre-lab", "task-1")
    with pytest.raises(ToolPolicyError, match="unexpected parameters"):
        policy.authorize("list_pods", {"namespace": "other"}, scope)
    with pytest.raises(ToolPolicyError, match="single line"):
        policy.authorize("query_metrics", {"query": "up\nmalicious"}, scope)


def test_client_exposes_minimal_schema_and_hides_cross_namespace_listing() -> None:
    async def inspect() -> tuple[set[str], dict]:
        server = FastMCP("policy-test")

        @server.tool(name="query_metrics")
        async def query_metrics(query: str | None = None, service: str | None = None) -> dict:
            return {"query": query, "service": service}

        client = FastMCPToolClient(server, policy=build_policy())
        specifications = await client.specifications()
        item = next(spec for spec in specifications if spec["name"] == "query_metrics")
        return {spec["name"] for spec in specifications}, item["input_schema"]

    names, schema = asyncio.run(inspect())
    assert "list_namespaces" not in names
    assert set(schema["properties"]) == {"query", "time_range_minutes"}
    assert schema["additionalProperties"] is False


def test_denied_tool_never_reaches_mcp_server() -> None:
    called = False

    async def run() -> None:
        nonlocal called
        server = FastMCP("deny-test")

        @server.tool(name="dangerous_write")
        async def dangerous_write() -> dict:
            nonlocal called
            called = True
            return {}

        client = FastMCPToolClient(server, policy=build_policy())
        with task_security_scope("u1", "sre-lab", "task-1"):
            with pytest.raises(ToolExecutionError, match="Tool Policy denied"):
                await client.execute("dangerous_write", {})

    asyncio.run(run())
    assert called is False


def test_sandbox_arguments_apply_all_resource_and_network_limits(tmp_path: Path) -> None:
    manager = DockerSandboxManager(
        tmp_path,
        image="python:3.12-alpine",
        cpus=0.5,
        memory_mb=256,
        pids_limit=64,
        timeout_seconds=30,
    )
    arguments = manager.docker_arguments("task-1", ["python", "-V"])
    joined = " ".join(arguments)
    assert "--network none" in joined
    assert "--cpus 0.5" in joined
    assert "--memory 256m" in joined
    assert "--pids-limit 64" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges:true" in joined
    assert "--read-only" in arguments
    with pytest.raises(SandboxError):
        manager.docker_arguments("../escape", ["python", "-V"])


def test_task_workspace_is_unique_and_removed_after_task(tmp_path: Path) -> None:
    manager = DockerSandboxManager(tmp_path, image="python:3.12-alpine")

    async def use_workspace() -> Path:
        async with manager.task_workspace("task-2") as workspace:
            assert workspace.is_dir()
            return workspace

    workspace = asyncio.run(use_workspace())
    assert not workspace.exists()


def test_audit_log_records_task_identity_parameters_and_status() -> None:
    database = mysql_test_database()
    initialize_audit_schema(database)
    repository = ToolAuditRepository(database)
    scope = TaskSecurityScope("u1", "sre-lab", "audit-task")

    audit_id = repository.record(
        scope,
        "query_metrics",
        {"query": "up", "authorization": "must-not-be-stored"},
        "success",
        12,
    )

    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM tool_audit_logs WHERE id = ?", (audit_id,)
        ).fetchone()
    assert row is not None
    assert row["user_id"] == "u1"
    assert row["project_id"] == "sre-lab"
    assert row["task_id"] == "audit-task"
    assert row["result_status"] == "success"
    assert "must-not-be-stored" not in str(row["parameters_json"])
