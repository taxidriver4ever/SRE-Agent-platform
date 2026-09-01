"""服务目录与 Kubernetes 资源浏览 API。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.router import get_sandbox_manager, get_tool_policy, require_project
from app.auth import CurrentUser, require_user
from app.evidence import build_source_references, normalize_tool_result
from app.mcp_clients import FastMCPToolClient
from app.sandbox import DockerSandboxManager
from app.security import ToolPolicy, task_security_scope
from app.workflow import DiagnosisWorkflow

router = APIRouter(prefix="/api", tags=["resources"])


def get_workflow(request: Request) -> DiagnosisWorkflow:
    return request.app.state.diagnosis_workflow


def get_tools(request: Request) -> FastMCPToolClient:
    return request.app.state.tools


@router.get("/services")
async def list_services(
    user: Annotated[CurrentUser, Depends(require_user)],
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
) -> dict[str, Any]:
    """从后端 Service Catalog 返回稳定元数据；运行指标由观测接口产生。"""
    del user
    services = []
    for name, metadata in workflow.catalog.services.items():
        upstreams = [
            source for source, source_metadata in workflow.catalog.services.items()
            if name in source_metadata.get("dependencies", [])
        ]
        services.append({
            "id": name,
            "name": name,
            "description": metadata.get("description", ""),
            "owner": metadata.get("owner", "unknown"),
            "runtime": " / ".join(filter(None, [metadata.get("language"), metadata.get("framework")])),
            "dependencies": metadata.get("dependencies", []),
            "upstreams": upstreams,
            "port": metadata.get("port"),
            "status": "unknown",
            "metrics": {"p95_ms": None, "error_rate": None, "cpu_percent": None, "memory_percent": None},
            "updated_at": None,
        })
    return {"items": services, "source": "service-catalog"}


@router.get("/services/{service_name}")
async def get_service(
    service_name: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
) -> dict[str, Any]:
    result = await list_services(user, workflow)
    item = next((service for service in result["items"] if service["id"] == service_name), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service not found")
    return item


@router.get("/services/{service_name}/pods")
async def list_service_pods(
    service_name: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
    tools: Annotated[FastMCPToolClient, Depends(get_tools)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    sandbox: Annotated[DockerSandboxManager, Depends(get_sandbox_manager)],
) -> dict[str, Any]:
    """通过第三方只读 Kubernetes MCP 实时列出 Service 对应 Pod。"""
    if service_name not in workflow.catalog.services:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service not found")
    project_id = "sre-lab"
    require_project(policy, project_id)
    task_id = uuid4().hex
    try:
        async with sandbox.task_workspace(task_id) as workspace:
            with task_security_scope(user["id"], project_id, task_id, str(workspace)):
                arguments = {"label_selector": f"app={service_name}"}
                raw = await tools.execute("list_pods", arguments)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Kubernetes Pod 数据暂不可用：{detail}",
        ) from exc
    references = build_source_references("list_pods", arguments, raw, namespace=workflow.kubernetes_namespace)
    normalized = normalize_tool_result("list_pods", arguments, raw, references)
    return {
        "service": service_name,
        "namespace": workflow.kubernetes_namespace,
        "pods": normalized.structured_data.get("pods", []),
        "summary": normalized.summary,
        "source_references": [item.model_dump(mode="json") for item in references],
    }


@router.get("/pods/{pod_name}")
async def get_pod(
    pod_name: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
    tools: Annotated[FastMCPToolClient, Depends(get_tools)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    sandbox: Annotated[DockerSandboxManager, Depends(get_sandbox_manager)],
) -> dict[str, Any]:
    """读取 Pod 详情；该端点和诊断工具使用相同只读策略。"""
    project_id = "sre-lab"
    require_project(policy, project_id)
    task_id = uuid4().hex
    try:
        async with sandbox.task_workspace(task_id) as workspace:
            with task_security_scope(user["id"], project_id, task_id, str(workspace)):
                raw = await tools.execute("get_pod", {"name": pod_name})
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Pod 数据暂不可用：{detail}") from exc
    references = build_source_references(
        "get_pod", {"name": pod_name}, raw, namespace=workflow.kubernetes_namespace,
    )
    return {
        "name": pod_name, "namespace": workflow.kubernetes_namespace,
        "data": raw,
        "source_references": [item.model_dump(mode="json") for item in references],
    }
