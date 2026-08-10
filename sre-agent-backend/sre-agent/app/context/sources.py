"""把工具输入/输出转换为稳定、可追溯的 Source Reference。"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field


class SourceReference(BaseModel):
    """指向证据原始来源，而不是指向已经裁剪后的摘要文本。"""

    # object_storage 用于用户直传的日志/附件；URI 只暴露私有对象 Key，不暴露签名。
    kind: Literal["kubernetes", "git", "metrics", "logs", "trace", "database", "object_storage"]
    uri: str
    label: str
    repository_url: str | None = None
    commit: str | None = None
    path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


def build_source_references(
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    *,
    namespace: str,
    repository_url: str | None,
) -> list[SourceReference]:
    """根据工具种类生成 URI；无法精确定位时宁可少引用，也不伪造行号。"""
    if tool_name in {"list_pods", "list_deployments", "get_pod", "get_pod_events", "get_restart_count", "get_container_image", "get_deployment"}:
        name = str(arguments.get("name") or arguments.get("label_selector") or "all")
        return [SourceReference(
            kind="kubernetes",
            uri=f"k8s://{quote(namespace)}/{quote(tool_name)}/{quote(name)}",
            label=f"Kubernetes {namespace}/{name}",
        )]
    if tool_name in {"read_file", "read_file_at_commit", "get_commit", "get_commit_diff", "list_changed_files", "search_code", "get_previous_commit", "get_repository", "get_current_commit"}:
        repository = str(arguments.get("repository") or "unknown")
        commit = str(arguments.get("commit") or arguments.get("head") or "HEAD")
        path = str(arguments.get("path") or "") or None
        base_uri = repository_url or f"repository://{quote(repository)}"
        suffix = f"/{quote(path)}" if path else ""
        return [SourceReference(
            kind="git",
            uri=f"git+{base_uri}@{quote(commit, safe='')}{suffix}",
            label=f"{repository}@{commit[:12]}{('/' + path) if path else ''}",
            repository_url=repository_url,
            commit=commit,
            path=path,
        )]
    if tool_name in {"query_metrics", "get_service_health"}:
        query = str(arguments.get("query") or arguments.get("service") or "health")
        return [SourceReference(kind="metrics", uri=f"prometheus://query/{quote(query, safe='')}", label="Prometheus query")]
    if tool_name == "query_logs":
        service = str(arguments.get("service") or "all")
        return [SourceReference(kind="logs", uri=f"loki://service/{quote(service)}", label=f"Loki {service}")]
    if tool_name == "query_trace":
        trace = str(arguments.get("trace_id") or arguments.get("service") or "search")
        return [SourceReference(kind="trace", uri=f"tempo://trace/{quote(trace)}", label=f"Tempo {trace}")]
    if tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}:
        return [SourceReference(kind="database", uri=f"mysql://sre_lab/{quote(tool_name)}", label=f"MySQL {tool_name}")]
    return []
