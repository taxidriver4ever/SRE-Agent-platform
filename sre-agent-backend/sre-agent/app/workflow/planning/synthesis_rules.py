"""可独立测试的 Evidence Pattern 综合规则。"""

from collections.abc import Callable
from typing import Any

from app.workflow.models import DiagnosisState, DiagnosisSynthesis, Evidence

SynthesisRule = Callable[[DiagnosisState], DiagnosisSynthesis | None]


def _direct(state: DiagnosisState, *, source: str | None = None, tool: str | None = None) -> list[Evidence]:
    return [item for item in state.evidence
            if item.direct_evidence and item.supports_conclusion
            and (source is None or item.source == source)
            and (tool is None or item.tool_name == tool)]


def kubernetes_restart_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    kubernetes = _direct(state, source="Kubernetes")
    pod_details = [item for item in kubernetes if item.tool_name == "get_pod"]
    events = [item for item in kubernetes if item.tool_name == "get_pod_events"]
    restarts = [item for item in kubernetes if item.tool_name == "get_restart_count"]
    liveness = next((item for item in reversed(events) if any(
        "liveness probe failed" in str(message).lower()
        for message in item.structured_data.get("runtime_messages", []))), None)
    invalid = next((item for item in reversed(pod_details) if any(
        path not in {"/actuator/health/liveness", "/health", "/healthz"}
        for path in item.structured_data.get("probe_paths", []))), None)
    if state.symptom != "pod_restart" or not liveness or not invalid or not restarts:
        return None
    invalid_path = next(path for path in invalid.structured_data.get("probe_paths", [])
                        if path not in {"/actuator/health/liveness", "/health", "/healthz"})
    return DiagnosisSynthesis(
        status="confirmed",
        root_cause=f"Kubernetes liveness probe（健康检查）路径 {invalid_path} 配置错误，探针返回 500 并触发容器反复重启",
        evidence_ids=[invalid.evidence_id, liveness.evidence_id, restarts[-1].evidence_id],
        root_cause_chain=[f"Pod Spec 的 liveness probe 指向 {invalid_path}", "Kubernetes Event 明确记录 Liveness probe failed: HTTP 500", "restart count 证实 kubelet 随后重启容器"],
        recommended_fix=["把 liveness path 恢复为应用真实健康端点并核对端口", "发布后观察 Events、Ready 状态和 restart count 不再增长"],
        confidence=0.98,
    )


def single_pod_degradation_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    if state.mixed_versions or not any(term in state.query.lower() for term in ("某个实例", "有时候", "间歇", "intermittent", "single pod")):
        return None
    metric = next((item for item in reversed(_direct(state, source="Prometheus")) if len([
        sample for sample in item.structured_data.get("metric_samples", [])
        if isinstance(sample, dict) and sample.get("labels", {}).get("pod")]) >= 2), None)
    inventory = next((item for item in reversed(state.evidence)
        if item.source == "Kubernetes" and item.tool_name == "list_pods" and item.supports_conclusion), None)
    if not metric or not inventory:
        return None
    samples = [sample for sample in metric.structured_data["metric_samples"] if sample.get("labels", {}).get("pod")]
    highest = max(samples, key=lambda sample: float(sample["value"]))
    baseline = min(float(sample["value"]) for sample in samples)
    ratio = float(highest["value"]) / max(baseline, 1e-12)
    if ratio < 1.25:
        return None
    pod = str(highest["labels"]["pod"])
    return DiagnosisSynthesis(
        status="confirmed",
        root_cause=f"单 Pod 实例退化：{pod} 的 CPU 约为同服务最低实例的 {ratio:.2f} 倍，导致请求命中该实例时间歇性变慢",
        evidence_ids=[inventory.evidence_id, metric.evidence_id],
        root_cause_chain=["Kubernetes 确认同一 Service 下存在多个运行实例", f"Prometheus Pod 维度指标显示 {pod} CPU 明显偏高", "负载均衡命中不同 Pod，形成时快时慢的间歇现象"],
        recommended_fix=["隔离该 Pod 并对比其线程、CPU profile 与兄弟实例", "修复后按 Pod 维度复测 CPU、P95 和请求分布"],
        confidence=0.9,
    )


def resource_exhaustion_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    metrics = _direct(state, source="Prometheus")
    oom = [item for item in _direct(state, source="Kubernetes") if any(
        "oomkilled" in str(reason).lower() for reason in item.structured_data.get("runtime_reasons", []))]
    if not oom or not metrics:
        return None
    return DiagnosisSynthesis(
        status="confirmed", root_cause="容器内存持续增长并超过 memory limit，触发 OOMKilled 后由 Kubernetes 重启",
        evidence_ids=[oom[-1].evidence_id, metrics[-1].evidence_id],
        root_cause_chain=["Prometheus 记录 Pod 资源运行指标", "Kubernetes 容器终止原因明确为 OOMKilled", "容器被重启并累计 restart count"],
        recommended_fix=["排查持续保留的 Buffer/对象并设置内存剖析与泄漏告警", "修复后观察 working_set、OOMKilled 事件和 restart count"],
        confidence=0.95,
    )


def cpu_saturation_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    logs = _direct(state, tool="query_logs")
    metrics = _direct(state, source="Prometheus")
    reasons = " ".join(str(reason) for item in state.evidence for reason in item.structured_data.get("runtime_reasons", [])).lower()
    if "cpu_saturation" not in reasons or not logs or not metrics:
        return None
    return DiagnosisSynthesis(
        status="confirmed", root_cause="CPU saturation：CPU 密集型计算阻塞服务工作线程，导致接口延迟升高",
        evidence_ids=[logs[-1].evidence_id, metrics[-1].evidence_id],
        root_cause_chain=["Loki 记录 cpu_saturation 故障模式下的业务请求", "Prometheus 记录 Pod CPU 与请求延迟运行指标", "CPU 密集计算导致可用工作线程下降"],
        recommended_fix=["把 CPU 密集计算移出请求线程或拆分到独立 Worker", "设置 CPU 限额、并发保护并复测 Pod CPU 与 P95"],
        confidence=0.9,
    )


def retry_storm_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    if not any(term in state.query.lower() for term in ("重试", "retry", "放大", "storm")):
        return None
    traces = _direct(state, tool="query_trace")
    logs = _direct(state, tool="query_logs")
    metrics = _direct(state, source="Prometheus")
    candidates = [candidate for item in traces for candidate in item.structured_data.get("dependency_candidates", [])
                  if isinstance(candidate, dict) and candidate.get("service") != state.service]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate.get("service")), []).append(candidate)
    repeated_service, repeated_calls = max(grouped.items(), key=lambda item: len(item[1]), default=("", []))
    if repeated_service and len(repeated_calls) >= 3 and logs and metrics:
        trace = next(item for item in reversed(traces) if sum(
            candidate.get("service") == repeated_service
            for candidate in item.structured_data.get("dependency_candidates", []) if isinstance(candidate, dict)) >= 3)
        return DiagnosisSynthesis(
            status="confirmed", root_cause=f"无退避重试风暴：单次请求连续调用下游 {repeated_service} {len(repeated_calls)} 次，放大请求量并导致 timeout",
            evidence_ids=[trace.evidence_id, logs[-1].evidence_id, metrics[-1].evidence_id],
            root_cause_chain=["Loki 记录故障窗口内业务请求", f"Tempo 同一请求中发现 {len(repeated_calls)} 个 {repeated_service} 调用 Span", "Prometheus 记录重试期间的请求与资源指标"],
            recommended_fix=["设置总重试预算并使用指数退避、抖动和熔断", "复测单请求下游 Span 数和放大后的 QPS"], confidence=0.93)
    repeated_log: Evidence | None = None
    repeated_trace_id = ""
    repeated_count = 0
    for item in logs:
        for trace_id, count in item.structured_data.get("trace_id_counts", {}).items():
            if int(count) > repeated_count:
                repeated_log, repeated_trace_id, repeated_count = item, str(trace_id), int(count)
    if repeated_log and repeated_count >= 3 and traces and metrics:
        return DiagnosisSynthesis(
            status="confirmed", root_cause=f"无退避重试风暴：同一 trace_id 在末端下游日志重复出现 {repeated_count} 次，放大请求量并导致 timeout",
            evidence_ids=[traces[-1].evidence_id, repeated_log.evidence_id, metrics[-1].evidence_id],
            root_cause_chain=["Tempo 确认故障请求的跨服务 Trace", f"Loki 显示 trace_id {repeated_trace_id[:8]}… 在下游重复出现 {repeated_count} 次", "Prometheus 记录故障窗口内的请求与资源指标"],
            recommended_fix=["设置总重试预算并使用指数退避、抖动和熔断", "复测单请求的同 trace_id 下游日志条数与整体 QPS"], confidence=0.91)
    return None


def dependency_timeout_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    if state.symptom != "dependency_timeout":
        return None
    traces = _direct(state, tool="query_trace")
    logs = _direct(state, tool="query_logs")
    candidates = [candidate for item in traces for candidate in item.structured_data.get("dependency_candidates", [])
                  if isinstance(candidate, dict) and candidate.get("service") != state.service]
    if not candidates or not logs:
        return None
    slowest = max(candidates, key=lambda item: float(item.get("duration_ms") or 0))
    duration = float(slowest.get("duration_ms") or 0)
    if duration < 1000:
        return None
    trace = next(item for item in reversed(traces) if slowest in item.structured_data.get("dependency_candidates", []))
    downstream = str(slowest["service"])
    return DiagnosisSynthesis(
        status="confirmed", root_cause=f"{downstream} 下游依赖调用耗时 {duration:.0f}ms 并发生 timeout，导致上游请求变慢",
        evidence_ids=[trace.evidence_id, logs[-1].evidence_id],
        root_cause_chain=["Loki 记录上游请求运行上下文", f"Tempo 显示到 {downstream} 的客户端 Span 最慢", "下游超时传播为上游延迟"],
        recommended_fix=["检查下游服务处理时延并设置分层超时预算", "增加有限重试、退避和熔断，复测跨服务 Trace"], confidence=0.9)


def deployment_regression_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    if not any(term in state.query.lower() for term in ("发布", "回归", "deploy", "release", "regression", "版本")):
        return None
    diffs = [item for item in state.evidence if item.tool_name == "get_commit_diff" and item.supports_conclusion]
    explains = [item for item in state.evidence if item.tool_name == "explain_sql"]
    full_scan = next((item for item in reversed(explains) if any(
        str(row.get("type", "")).upper() == "ALL" and not row.get("key")
        for row in item.structured_data.get("rows", []) if isinstance(row, dict))), None)
    slow = next((item for item in reversed(state.evidence) if item.tool_name == "query_slow_queries" and item.supports_conclusion), None)
    pod = next((item for item in reversed(state.evidence) if item.source == "Kubernetes" and item.tool_name == "list_pods" and item.supports_conclusion), None)
    if not diffs or not full_scan or not slow or not pod:
        return None
    sql_text = " ".join(str(row.get("sql_text", "")) for row in slow.structured_data.get("rows", []) if isinstance(row, dict)).lower()
    shape = "LIKE 模糊匹配" if " like " in f" {sql_text} " else "不可索引的查询条件"
    return DiagnosisSynthesis(
        status="confirmed", root_cause=f"Git 发布代码回归引入{shape}与计算排序，导致索引失效、MySQL 全表扫描",
        evidence_ids=[pod.evidence_id, diffs[-1].evidence_id, slow.evidence_id, full_scan.evidence_id],
        root_cause_chain=["Kubernetes 运行 Pod 暴露当前部署 commit", "Git Diff 显示发布版本修改了订单查询实现", "MySQL 慢查询记录扫描约十万行", "EXPLAIN 显示 type=ALL 且未命中索引"],
        recommended_fix=["回滚该 Git 回归或移除不可索引的 LIKE/计算排序", "按查询模式补充可用索引并用 EXPLAIN、慢日志复测"], confidence=0.95)


def connection_pool_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    # A slow query may temporarily produce secondary Hikari wait/timeout logs.
    # Treat pool exhaustion as the primary cause only when the observed incident
    # is an error spike; latency-only incidents continue to the SQL-plan rule.
    if state.symptom != "5xx":
        return None
    slow = next((item for item in reversed(state.evidence) if item.tool_name == "query_slow_queries" and item.supports_conclusion), None)
    logs = _direct(state, tool="query_logs")
    text = " ".join([item.detail for item in logs] + [str(message) for item in logs for message in item.structured_data.get("runtime_messages", [])]).lower()
    if not slow or not logs or not any(marker in text for marker in (
        "cannotgetjdbcconnectionexception", "failed to obtain jdbc connection", "connection is not available", "hikari", "connection timeout")):
        return None
    return DiagnosisSynthesis(
        status="confirmed", root_cause="Hikari connection pool（连接池）耗尽，业务请求无法及时获得 JDBC Connection 并返回 500",
        evidence_ids=[logs[-1].evidence_id, slow.evidence_id],
        root_cause_chain=["Loki 记录 Failed to obtain JDBC Connection", "并发请求持续占用数据库连接", "Hikari 连接池等待超时并触发 5xx"],
        recommended_fix=["先优化占用连接时间长的查询，再按数据库容量校准连接池上限与超时", "复测 Hikari active/pending、5xx 和数据库查询耗时"], confidence=0.92)


def slow_query_rule(state: DiagnosisState) -> DiagnosisSynthesis | None:
    slow_items = [item for item in state.evidence if item.tool_name == "query_slow_queries"]
    explain_items = [item for item in state.evidence if item.tool_name == "explain_sql"]
    traces = _direct(state, tool="query_trace")
    for slow in slow_items:
        rows = slow.structured_data.get("rows", [])
        if not rows:
            continue
        sql = " ".join(str(row.get("sql_text") or "") for row in rows).lower()
        examined = max((int(row.get("rows_examined") or 0) for row in rows), default=0)
        for plan in explain_items:
            plan_rows = plan.structured_data.get("rows", [])
            if examined <= 0 or not any(str(row.get("type") or "").upper() == "ALL" for row in plan_rows) or not any(not row.get("key") for row in plan_rows):
                continue
            ids = [item.evidence_id for item in traces[-1:]] + [slow.evidence_id, plan.evidence_id]
            mechanism = "前导通配 LIKE 导致索引无法使用" if "like '%" in sql else "查询未使用可用索引"
            return DiagnosisSynthesis(
                status="confirmed", root_cause=f"慢 SQL 的{mechanism}，执行计划为 ALL 全表扫描并检查 {examined} 行",
                evidence_ids=list(dict.fromkeys(ids)),
                root_cause_chain=["Trace 定位到数据库查询", f"MySQL 慢查询记录检查 {examined} 行", "EXPLAIN 显示 type=ALL 且未命中索引"],
                recommended_fix=["移除前导通配查询或改用适合模糊检索的索引方案", "修复后用相同请求复测 P95、rows_examined 与 EXPLAIN"], confidence=0.95)
    return None


SYNTHESIS_RULES: tuple[SynthesisRule, ...] = (
    kubernetes_restart_rule,
    single_pod_degradation_rule,
    resource_exhaustion_rule,
    cpu_saturation_rule,
    retry_storm_rule,
    dependency_timeout_rule,
    deployment_regression_rule,
    connection_pool_rule,
    slow_query_rule,
)


def deterministic_synthesis(state: DiagnosisState) -> DiagnosisSynthesis | None:
    """按从具体到通用的顺序返回首个完整闭环的 Evidence Pattern。"""
    for rule in SYNTHESIS_RULES:
        result = rule(state)
        if result is not None:
            return result
    return None
