"""把不同 MCP 的只读结果归一化为可继续规划的证据对象。"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from app.evidence.references import SourceReference


_TRACE_ID = re.compile(r"^[0-9a-fA-F]{16,32}$")
_SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class UnifiedToolResult(BaseModel):
    """Workflow 内部和 Conversation Store 使用的统一 Tool Result。"""

    tool: str
    status: str = "success"
    summary: str
    data: Any
    structured_data: dict[str, Any] = Field(default_factory=dict)
    references: list[SourceReference] = Field(default_factory=list)
    next_hints: list[str] = Field(default_factory=list)


def normalize_tool_result(
    tool_name: str,
    arguments: dict[str, Any],
    raw_result: Any,
    references: list[SourceReference],
    *,
    summary_limit: int = 900,
) -> UnifiedToolResult:
    """保留有界原始结果，同时提取 Trace、SQL、服务和 Pod 等导航字段。"""
    signals: dict[str, list[Any]] = {
        "trace_ids": [],
        "sql_statements": [],
        "services": [],
        "pods": [],
        "commits": [],
        "runtime_reasons": [],
        "probe_paths": [],
        "trace_candidates": [],
    }
    _walk(raw_result, signals)
    for key, values in signals.items():
        signals[key] = _unique(values)[:20]

    hints: list[str] = []
    if signals["trace_ids"] and tool_name != "query_trace":
        hints.append("日志或事件中发现 trace_id；可按该 ID 精确查询 Trace")
    if signals["sql_statements"] and tool_name != "explain_sql":
        hints.append("运行时证据中发现只读 SQL；可对原始 SQL 执行 EXPLAIN")
    if signals["pods"] and tool_name == "list_pods":
        hints.append("已发现目标 Pod；如存在重启或健康异常，可读取 Pod、Events 和 restart count")
    if signals["commits"]:
        hints.append("已发现运行 commit；如怀疑发布回归，可读取提交、Diff 和 Code State")

    serialized = json.dumps(raw_result, ensure_ascii=False, default=str, sort_keys=True)
    summary = serialized if len(serialized) <= summary_limit else f"{serialized[:summary_limit]}…"
    return UnifiedToolResult(
        tool=tool_name,
        summary=summary,
        data=raw_result,
        structured_data={key: value for key, value in signals.items() if value},
        references=references,
        next_hints=hints,
    )


def _walk(value: Any, signals: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        trace_id = str(value.get("traceID") or value.get("trace_id") or "")
        trace_name = str(value.get("rootTraceName") or value.get("name") or "")
        if _TRACE_ID.fullmatch(trace_id) and trace_name:
            signals["trace_candidates"].append({
                "trace_id": trace_id,
                "name": trace_name[:300],
                "duration_ms": value.get("durationMs"),
            })
        attribute_key = str(value.get("key") or "")
        if attribute_key:
            attribute_value = _otel_value(value.get("value"))
            _capture(attribute_key, attribute_value, signals)
        for key, child in value.items():
            _capture(str(key), child, signals)
            _walk(child, signals)
    elif isinstance(value, list):
        for child in value:
            _walk(child, signals)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                _walk(json.loads(stripped), signals)
            except json.JSONDecodeError:
                pass


def _capture(key: str, value: Any, signals: dict[str, list[Any]]) -> None:
    lowered = key.lower().replace("-", "_")
    scalar = _scalar(value)
    if scalar is None:
        return
    text = str(scalar).strip()
    if lowered in {"trace_id", "traceid"} and _TRACE_ID.fullmatch(text):
        signals["trace_ids"].append(text)
    elif lowered in {"db.statement", "db_statement", "sql", "sql_text", "digest_text", "statement"}:
        if _SQL_START.match(text) and len(text) <= 4000:
            signals["sql_statements"].append(text.rstrip(";"))
    elif lowered in {"service", "service.name", "service_name", "peer.service"} and text:
        signals["services"].append(text)
    elif lowered == "name" and ("-" in text or text.endswith("pod")):
        signals["pods"].append(text)
    elif lowered in {"commit", "git_sha", "git.commit.sha", "sre.agent/git_sha", "sre.agent/git-sha"}:
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
            signals["commits"].append(text)
    elif lowered in {"reason", "lastterminationstate", "restart_count", "restartcount"}:
        signals["runtime_reasons"].append(text[:300])
    elif lowered in {"path", "httpget.path", "liveness_path", "readiness_path"} and text.startswith("/"):
        signals["probe_paths"].append(text)


def _otel_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return value


def _scalar(value: Any) -> Any | None:
    if isinstance(value, (str, int, float, bool)):
        return value
    return _otel_value(value) if isinstance(value, dict) else None


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
