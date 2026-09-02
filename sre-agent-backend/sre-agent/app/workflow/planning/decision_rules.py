"""只消费用户、Catalog 与 Evidence 的确定性下一步规则。"""

import json
import re
from typing import Any

from app.workflow.models import DiagnosisState
from app.workflow.planning.models import PlannerDecision


def is_explainable_sql(sql: str) -> bool:
    """仅允许不含未绑定参数的只读 SQL 进入 EXPLAIN。"""
    text = sql.strip()
    if not re.match(r"(?is)^select\b", text):
        return False
    return not re.search(r"(?<![\w])\?|:\w+|\$\d+", text)


def evidence_driven_decision(state: DiagnosisState) -> PlannerDecision | None:
    """按关联 Evidence 选择下一跳；没有通用规则命中时交给 LLM fallback。"""
    attempted = {(item.tool_name, json.dumps(item.arguments, sort_keys=True, default=str)) for item in state.timeline}

    def called(tool: str, arguments: dict[str, Any] | None = None) -> bool:
        if arguments is None:
            return any(name == tool for name, _ in attempted)
        return (tool, json.dumps(arguments, sort_keys=True, default=str)) in attempted

    def parents(predicate: Any) -> list[str]:
        return [item.evidence_id for item in state.evidence if predicate(item)][-4:]

    trace_ids: list[str] = []
    sql_statements: list[str] = []
    discovered_services: list[str] = []
    for item in state.evidence:
        if item.tool_name != "query_trace":
            trace_ids.extend(item.structured_data.get("trace_ids", []))
        sql_statements.extend(item.structured_data.get("sql_statements", []))
        discovered_services.extend(item.structured_data.get("services", []))

    for trace_id in list(dict.fromkeys(trace_ids))[:1]:
        arguments = {"trace_id": trace_id}
        if not called("query_trace", arguments):
            return PlannerDecision(action="tool", tool_name="query_trace", arguments=arguments,
                title="按日志关联 ID 精确读取 Trace", reason="现有运行时证据包含 trace_id",
                parent_evidence_ids=parents(lambda item: trace_id in item.structured_data.get("trace_ids", [])))

    for evidence_item in reversed(state.evidence):
        if evidence_item.tool_name != "query_trace":
            continue
        candidates = [candidate for candidate in evidence_item.structured_data.get("trace_candidates", [])
            if isinstance(candidate, dict) and candidate.get("trace_id")
            and not any(marker in str(candidate.get("name", "")).lower() for marker in ("health", "prometheus", "metrics"))]
        if candidates:
            selected = max(candidates, key=lambda item: float(item.get("duration_ms") or 0))
            arguments = {"trace_id": selected["trace_id"]}
            if not called("query_trace", arguments):
                return PlannerDecision(action="tool", tool_name="query_trace", arguments=arguments,
                    title="读取最慢业务 Trace 详情", reason="Trace 搜索结果包含尚未展开的非健康检查业务请求",
                    parent_evidence_ids=[evidence_item.evidence_id])

    if state.symptom in {"latency", "5xx"} and not called("query_slow_queries"):
        return PlannerDecision(action="tool", tool_name="query_slow_queries",
            arguments={"time_range_minutes": state.time_range_minutes, "limit": 10},
            title="检查近期数据库慢查询", reason="延迟或错误基线需要排除数据库等待",
            parent_evidence_ids=parents(lambda item: item.source in {"Prometheus", "Loki", "Tempo"}))

    for sql in dict.fromkeys(sql_statements):
        if not is_explainable_sql(sql):
            continue
        arguments = {"sql": sql}
        if not called("explain_sql", arguments):
            return PlannerDecision(action="tool", tool_name="explain_sql", arguments=arguments,
                title="验证运行时 SQL 执行计划", reason="Trace 或数据库证据包含原始只读 SQL",
                parent_evidence_ids=parents(lambda item: sql in item.structured_data.get("sql_statements", [])))

    if any(term in state.query.lower() for term in ("重试", "retry", "放大", "storm")) and state.dependencies:
        for downstream_service in state.dependencies:
            arguments = {"service": downstream_service, "limit": 10}
            if not called("query_trace", arguments):
                return PlannerDecision(action="tool", tool_name="query_trace", arguments=arguments,
                    title="搜索下游服务 Trace", reason="用户询问重试放大，需要沿服务目录逐级验证下游调用",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "query_trace"))
        for downstream_service in reversed(state.dependencies):
            arguments = {"service": downstream_service, "time_range_minutes": state.time_range_minutes, "limit": 20}
            if not called("query_logs", arguments):
                return PlannerDecision(action="tool", tool_name="query_logs", arguments=arguments,
                    title="读取末端下游服务日志", reason="需要核对同一 trace_id 是否因无退避重试而重复出现",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "query_trace"))

    if state.symptom == "pod_restart" and state.pod_name:
        for tool, title in (("get_pod", "读取异常 Pod 状态与探针"), ("get_pod_events", "读取异常 Pod Events"), ("get_restart_count", "读取容器重启次数")):
            arguments = {"name": state.pod_name}
            if not called(tool, arguments):
                return PlannerDecision(action="tool", tool_name=tool, arguments=arguments, title=title,
                    reason="基线证据显示目标服务存在重启现象",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "list_pods"))

    if state.symptom in {"latency", "dependency_timeout"} and not called("query_trace"):
        return PlannerDecision(action="tool", tool_name="query_trace", arguments={"service": state.service, "limit": 10},
            title="搜索目标服务近期 Trace", reason="需要把请求耗时归属到具体 Span",
            parent_evidence_ids=parents(lambda item: item.source in {"Prometheus", "Loki"}))

    downstream = next((service for service in dict.fromkeys(discovered_services) if service != state.service), None)
    downstream_arguments = {"service": downstream, "time_range_minutes": state.time_range_minutes, "limit": 20} if downstream else None
    if state.symptom == "dependency_timeout" and downstream and not called("query_logs", downstream_arguments):
        return PlannerDecision(action="tool", tool_name="query_logs", arguments=downstream_arguments or {},
            title="交叉验证下游服务日志", reason="运行时证据发现下游服务名",
            parent_evidence_ids=parents(lambda item: downstream in item.structured_data.get("services", [])))

    regression = state.mixed_versions or any(term in state.query.lower() for term in ("发布", "回归", "deploy", "release", "regression", "版本"))
    if regression and state.repository and state.runtime_commit:
        commit_arguments = {"repository": state.repository, "commit": state.runtime_commit}
        if not called("get_commit", commit_arguments):
            return PlannerDecision(action="tool", tool_name="get_commit", arguments=commit_arguments,
                title="读取运行版本提交元数据", reason="运行对象给出了 repository 与 commit",
                parent_evidence_ids=parents(lambda item: item.source == "Kubernetes"))
        diff_arguments = {"repository": state.repository, "base": f"{state.runtime_commit}^", "head": state.runtime_commit}
        if not called("get_commit_diff", diff_arguments):
            return PlannerDecision(action="tool", tool_name="get_commit_diff", arguments=diff_arguments,
                title="比较运行提交与前一版本", reason="症状与版本变化相关，需要验证实际 Diff",
                parent_evidence_ids=parents(lambda item: item.tool_name == "get_commit"))

    if len([item for item in state.evidence if item.direct_evidence and item.supports_conclusion]) >= 2:
        return PlannerDecision(action="finish", reason="已有至少两条可用于综合的直接运行时证据")
    return None
