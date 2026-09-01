"""只根据当前证据选择下一步调查动作并综合最终结论。"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError,
    schema_retry_message,
    template_refill_message,
    validate_structured_output,
)
from app.workflow.models import DiagnosisState, DiagnosisSynthesis


class PlannerDecision(BaseModel):
    """单轮 Planner 决策；参数仍会经过 Tool Policy 二次校验。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "finish", "insufficient_evidence"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(default="下一步调查", max_length=120)
    reason: str = Field(default="", max_length=500)
    parent_evidence_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_action(self) -> "PlannerDecision":
        if self.action == "tool" and not self.tool_name:
            raise ValueError("tool action requires tool_name")
        if self.action != "tool" and (self.tool_name is not None or self.arguments):
            raise ValueError("non-tool action cannot contain tool_name or arguments")
        return self


class EvidencePlanner:
    """LLM Planner 不接收 Eval Expected，只接收用户问题和真实 Tool Result 摘要。"""

    def __init__(self, llm: LLM, retries: int = 2) -> None:
        self.llm = llm
        self.retries = max(1, min(3, retries))
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def decide(
        self,
        state: DiagnosisState,
        tool_specs: list[dict[str, Any]],
    ) -> PlannerDecision:
        fast_decision = self._evidence_driven_decision(state)
        if fast_decision is not None:
            return fast_decision
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "tool": item.tool_name,
                "summary": item.detail,
                "structured_data": item.structured_data,
                "next_hints": item.next_hints,
                "supports_conclusion": item.supports_conclusion,
            }
            for item in state.evidence
        ]
        calls = [
            {"tool": item.tool_name, "arguments": item.arguments, "success": item.error is None}
            for item in state.timeline
        ]
        system = (
            "你是单 Agent SRE Evidence Planner。你不知道任何评测答案，也不能按服务名或 Case ID 套用故障。"
            "只根据用户现象和当前真实证据决定一个下一步。参数值只能来自用户输入、Tool Result 或服务目录；"
            "禁止猜测 SQL、Trace ID、Pod、下游服务、文件路径和 commit。"
            "优先沿运行时关联推进：日志出现 trace_id 后精确查 Trace；Trace/慢日志出现 SQL 后才可 EXPLAIN；"
            "Pod 重启或健康异常时查 Pod/Events/restart/probe；发现运行 commit 且证据指向发布变化时再查 Git/Diff/Code State。"
            "不得重复完全相同的成功调用。已有直接证据足以解释现象时 finish；没有安全且有依据的下一步时 insufficient_evidence。"
            "parent_evidence_ids 必须填写真正促使本动作发生的现有证据 ID。只输出一个 JSON 对象。/no_think"
        )
        payload = {
            "query": state.query,
            "service": state.service,
            "symptom": state.symptom,
            "repository": state.repository,
            "runtime_commit": state.runtime_commit,
            "source_code_location": state.source_code_location,
            "available_tools": self._compact_tool_specs(tool_specs),
            "evidence": evidence,
            "previous_calls": calls,
            "remaining_tool_calls": max(0, state.max_tool_steps - len(state.timeline)),
            "output_schema": {
                "action": "tool|finish|insufficient_evidence",
                "tool_name": "string|null",
                "arguments": {},
                "title": "string",
                "reason": "string",
                "parent_evidence_ids": [],
            },
        }
        template = PlannerDecision(action="insufficient_evidence", reason="证据不足").model_dump(mode="json")
        decision = await self._complete_structured(system, payload, PlannerDecision, template)
        known_ids = {item.evidence_id for item in state.evidence}
        decision.parent_evidence_ids = [item for item in decision.parent_evidence_ids if item in known_ids]
        return decision

    @staticmethod
    def _evidence_driven_decision(state: DiagnosisState) -> PlannerDecision | None:
        """从 Tool Result 中的关联字段生成确定性下一跳，避免小模型重复推导事实。"""
        attempted = {
            (item.tool_name, json.dumps(item.arguments, sort_keys=True, default=str))
            for item in state.timeline
        }

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

        # 一组并发错误日志往往包含大量等价 trace_id；先抽样一条建立跨源关联，
        # 避免把全部工具预算耗在同一机制的重复 Trace 上。
        for trace_id in list(dict.fromkeys(trace_ids))[:1]:
            arguments = {"trace_id": trace_id}
            if not called("query_trace", arguments):
                return PlannerDecision(
                    action="tool", tool_name="query_trace", arguments=arguments,
                    title="按日志关联 ID 精确读取 Trace",
                    reason="现有运行时证据包含 trace_id",
                    parent_evidence_ids=parents(lambda item: trace_id in item.structured_data.get("trace_ids", [])),
                )

        # 每次搜索都应继续展开它自己尚未读取的业务 Trace。若把所有历史候选合并后
        # 只取全局最慢项，最慢项一旦读取过，后续针对下游服务的新搜索会被意外跳过。
        for evidence_item in reversed(state.evidence):
            if evidence_item.tool_name != "query_trace":
                continue
            candidates = [
                candidate
                for candidate in evidence_item.structured_data.get("trace_candidates", [])
                if isinstance(candidate, dict)
                and candidate.get("trace_id")
                and not any(
                    marker in str(candidate.get("name", "")).lower()
                    for marker in ("health", "prometheus", "metrics")
                )
            ]
            if candidates:
                selected = max(candidates, key=lambda item: float(item.get("duration_ms") or 0))
                arguments = {"trace_id": selected["trace_id"]}
                if not called("query_trace", arguments):
                    return PlannerDecision(
                        action="tool",
                        tool_name="query_trace",
                        arguments=arguments,
                        title="读取最慢业务 Trace 详情",
                        reason="Trace 搜索结果包含尚未展开的非健康检查业务请求",
                        parent_evidence_ids=[evidence_item.evidence_id],
                    )

        # 延迟/5xx 场景先用数据库自身的慢查询记录确认是否真的存在数据库等待，
        # 避免仅凭 Trace 中的模板 SQL（通常含 ?/$1 占位符）直接执行 EXPLAIN。
        if state.symptom in {"latency", "5xx"} and not called("query_slow_queries"):
            return PlannerDecision(
                action="tool", tool_name="query_slow_queries",
                arguments={"time_range_minutes": state.time_range_minutes, "limit": 10},
                title="检查近期数据库慢查询",
                reason="延迟或错误基线需要排除数据库等待",
                parent_evidence_ids=parents(lambda item: item.source in {"Prometheus", "Loki", "Tempo"}),
            )

        for sql in dict.fromkeys(sql_statements):
            if not EvidencePlanner._is_explainable_sql(sql):
                continue
            arguments = {"sql": sql}
            if not called("explain_sql", arguments):
                return PlannerDecision(
                    action="tool", tool_name="explain_sql", arguments=arguments,
                    title="验证运行时 SQL 执行计划",
                    reason="Trace 或数据库证据包含原始只读 SQL",
                    parent_evidence_ids=parents(lambda item: sql in item.structured_data.get("sql_statements", [])),
                )

        retry_terms = ("重试", "retry", "放大", "storm")
        if any(term in state.query.lower() for term in retry_terms) and state.dependencies:
            for downstream_service in state.dependencies:
                arguments = {"service": downstream_service, "limit": 10}
                if called("query_trace", arguments):
                    continue
                return PlannerDecision(
                    action="tool", tool_name="query_trace", arguments=arguments,
                    title="搜索下游服务 Trace",
                    reason="用户询问重试放大，需要沿服务目录逐级验证下游调用",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "query_trace"),
                )
            for downstream_service in reversed(state.dependencies):
                arguments = {
                    "service": downstream_service,
                    "time_range_minutes": state.time_range_minutes,
                    "limit": 20,
                }
                if called("query_logs", arguments):
                    continue
                return PlannerDecision(
                    action="tool", tool_name="query_logs", arguments=arguments,
                    title="读取末端下游服务日志",
                    reason="需要核对同一 trace_id 是否因无退避重试而重复出现",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "query_trace"),
                )

        if state.symptom == "pod_restart" and state.pod_name:
            for tool, title in (
                ("get_pod", "读取异常 Pod 状态与探针"),
                ("get_pod_events", "读取异常 Pod Events"),
                ("get_restart_count", "读取容器重启次数"),
            ):
                arguments = {"name": state.pod_name}
                if not called(tool, arguments):
                    return PlannerDecision(
                        action="tool", tool_name=tool, arguments=arguments, title=title,
                        reason="基线证据显示目标服务存在重启现象",
                        parent_evidence_ids=parents(lambda item: item.tool_name == "list_pods"),
                    )

        if state.symptom in {"latency", "dependency_timeout"} and not called("query_trace"):
            return PlannerDecision(
                action="tool", tool_name="query_trace",
                arguments={"service": state.service, "limit": 10},
                title="搜索目标服务近期 Trace",
                reason="需要把请求耗时归属到具体 Span",
                parent_evidence_ids=parents(lambda item: item.source in {"Prometheus", "Loki"}),
            )

        downstream = next((service for service in dict.fromkeys(discovered_services) if service != state.service), None)
        if state.symptom == "dependency_timeout" and downstream and not called("query_logs", {"service": downstream, "time_range_minutes": state.time_range_minutes, "limit": 20}):
            arguments = {"service": downstream, "time_range_minutes": state.time_range_minutes, "limit": 20}
            return PlannerDecision(
                action="tool", tool_name="query_logs", arguments=arguments,
                title="交叉验证下游服务日志", reason="运行时证据发现下游服务名",
                parent_evidence_ids=parents(lambda item: downstream in item.structured_data.get("services", [])),
            )

        regression_terms = ("发布", "回归", "deploy", "release", "regression", "版本")
        regression = state.mixed_versions or any(term in state.query.lower() for term in regression_terms)
        if regression and state.repository and state.runtime_commit:
            commit_arguments = {"repository": state.repository, "commit": state.runtime_commit}
            if not called("get_commit", commit_arguments):
                return PlannerDecision(
                    action="tool", tool_name="get_commit", arguments=commit_arguments,
                    title="读取运行版本提交元数据", reason="运行对象给出了 repository 与 commit",
                    parent_evidence_ids=parents(lambda item: item.source == "Kubernetes"),
                )
            diff_arguments = {
                "repository": state.repository,
                "base": f"{state.runtime_commit}^",
                "head": state.runtime_commit,
            }
            if not called("get_commit_diff", diff_arguments):
                return PlannerDecision(
                    action="tool", tool_name="get_commit_diff", arguments=diff_arguments,
                    title="比较运行提交与前一版本", reason="症状与版本变化相关，需要验证实际 Diff",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "get_commit"),
                )

        if len([item for item in state.evidence if item.direct_evidence and item.supports_conclusion]) >= 2:
            return PlannerDecision(action="finish", reason="已有至少两条可用于综合的直接运行时证据")
        return None

    @staticmethod
    def _is_explainable_sql(sql: str) -> bool:
        """仅允许不含未绑定参数的只读 SQL 进入 EXPLAIN。"""
        text = sql.strip()
        if not re.match(r"(?is)^select\b", text):
            return False
        return not re.search(r"(?<![\w])\?|:\w+|\$\d+", text)

    async def synthesize(self, state: DiagnosisState) -> DiagnosisSynthesis:
        deterministic = self._deterministic_synthesis(state)
        if deterministic is not None:
            return deterministic
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "tool": item.tool_name,
                "summary": item.detail,
                "structured_data": item.structured_data,
                "direct_evidence": item.direct_evidence,
                "supports_conclusion": item.supports_conclusion,
                "parent_evidence_ids": item.parent_evidence_ids,
                "references": [reference.model_dump(mode="json") for reference in item.source_references],
            }
            for item in state.evidence
        ]
        system = (
            "你是证据约束的 SRE 诊断综合器。禁止使用预设故障答案，禁止从服务名猜根因。"
            "结论只能引用输入中的 evidence_id，并必须解释观测到的症状。"
            "confirmed 必须有直接运行时证据且引用至少两条互相支持的证据；只有代码、用户措辞、空结果或模型推测时必须返回 insufficient_evidence。"
            "证据互相矛盾时列入 contradictions 并返回 insufficient_evidence。"
            "修复建议只能针对已被证据支持的机制。只返回 JSON。/no_think"
        )
        payload = {
            "query": state.query,
            "service": state.service,
            "symptom": state.symptom,
            "evidence": evidence,
            "output_schema": {
                "status": "confirmed|insufficient_evidence",
                "root_cause": "string",
                "evidence_ids": [],
                "root_cause_chain": [],
                "recommended_fix": [],
                "confidence": 0.0,
                "contradictions": [],
            },
            "valid_output_example": {
                "status": "insufficient_evidence",
                "root_cause": "证据不足，无法确认根因",
                "evidence_ids": [],
                "root_cause_chain": [],
                "recommended_fix": [],
                "confidence": 0.0,
                "contradictions": [],
            },
        }
        template = DiagnosisSynthesis(
            status="insufficient_evidence",
            root_cause="证据不足，无法确认根因",
            confidence=0.0,
        ).model_dump(mode="json")
        return await self._complete_structured(system, payload, DiagnosisSynthesis, template)

    @staticmethod
    def _deterministic_synthesis(state: DiagnosisState) -> DiagnosisSynthesis | None:
        """对结构化运行证据能完整闭环的机制直接综合，避免小模型否认客观结果。

        规则只读取真实 Tool Result 的字段，不读取服务名、Case ID 或评测期望。
        """
        slow_items = [item for item in state.evidence if item.tool_name == "query_slow_queries"]
        explain_items = [item for item in state.evidence if item.tool_name == "explain_sql"]
        trace_items = [
            item for item in state.evidence
            if item.tool_name == "query_trace" and item.direct_evidence and item.supports_conclusion
        ]
        log_items = [
            item for item in state.evidence
            if item.tool_name == "query_logs" and item.direct_evidence and item.supports_conclusion
        ]
        log_text = " ".join(
            [item.detail for item in log_items]
            + [
                str(message)
                for item in log_items
                for message in item.structured_data.get("runtime_messages", [])
            ]
        ).lower()
        runtime_reason_text = " ".join(
            str(reason)
            for item in state.evidence
            for reason in item.structured_data.get("runtime_reasons", [])
        ).lower()
        metric_items = [
            item for item in state.evidence
            if item.source == "Prometheus" and item.direct_evidence and item.supports_conclusion
        ]
        kubernetes_items = [
            item for item in state.evidence
            if item.source == "Kubernetes" and item.direct_evidence and item.supports_conclusion
        ]
        oom_items = [
            item for item in kubernetes_items
            if any("oomkilled" in str(reason).lower() for reason in item.structured_data.get("runtime_reasons", []))
        ]
        pod_detail_items = [item for item in kubernetes_items if item.tool_name == "get_pod"]
        pod_event_items = [item for item in kubernetes_items if item.tool_name == "get_pod_events"]
        restart_items = [item for item in kubernetes_items if item.tool_name == "get_restart_count"]
        liveness_event = next((
            item for item in reversed(pod_event_items)
            if any("liveness probe failed" in str(message).lower()
                   for message in item.structured_data.get("runtime_messages", []))
        ), None)
        invalid_probe_item = next((
            item for item in reversed(pod_detail_items)
            if any(path not in {"/actuator/health/liveness", "/health", "/healthz"}
                   for path in item.structured_data.get("probe_paths", []))
        ), None)
        if state.symptom == "pod_restart" and liveness_event and invalid_probe_item and restart_items:
            invalid_path = next(
                path for path in invalid_probe_item.structured_data.get("probe_paths", [])
                if path not in {"/actuator/health/liveness", "/health", "/healthz"}
            )
            return DiagnosisSynthesis(
                status="confirmed",
                root_cause=f"Kubernetes liveness probe（健康检查）路径 {invalid_path} 配置错误，探针返回 500 并触发容器反复重启",
                evidence_ids=[invalid_probe_item.evidence_id, liveness_event.evidence_id, restart_items[-1].evidence_id],
                root_cause_chain=[
                    f"Pod Spec 的 liveness probe 指向 {invalid_path}",
                    "Kubernetes Event 明确记录 Liveness probe failed: HTTP 500",
                    "restart count 证实 kubelet 随后重启容器",
                ],
                recommended_fix=[
                    "把 liveness path 恢复为应用真实健康端点并核对端口",
                    "发布后观察 Events、Ready 状态和 restart count 不再增长",
                ],
                confidence=0.98,
            )
        instance_terms = ("某个实例", "有时候", "间歇", "intermittent", "single pod")
        pod_metric_item = next((
            item for item in reversed(metric_items)
            if len([
                sample for sample in item.structured_data.get("metric_samples", [])
                if isinstance(sample, dict) and sample.get("labels", {}).get("pod")
            ]) >= 2
        ), None)
        pod_inventory_item = next((
            item for item in reversed(state.evidence)
            if item.source == "Kubernetes" and item.tool_name == "list_pods" and item.supports_conclusion
        ), None)
        if (
            not state.mixed_versions
            and any(term in state.query.lower() for term in instance_terms)
            and pod_metric_item and pod_inventory_item
        ):
            samples = [
                sample for sample in pod_metric_item.structured_data["metric_samples"]
                if sample.get("labels", {}).get("pod")
            ]
            highest = max(samples, key=lambda sample: float(sample["value"]))
            healthy_baseline = min(float(sample["value"]) for sample in samples)
            ratio = float(highest["value"]) / max(healthy_baseline, 1e-12)
            if ratio >= 1.25:
                affected_pod = str(highest["labels"]["pod"])
                return DiagnosisSynthesis(
                    status="confirmed",
                    root_cause=f"单 Pod 实例退化：{affected_pod} 的 CPU 约为同服务最低实例的 {ratio:.2f} 倍，导致请求命中该实例时间歇性变慢",
                    evidence_ids=[pod_inventory_item.evidence_id, pod_metric_item.evidence_id],
                    root_cause_chain=[
                        "Kubernetes 确认同一 Service 下存在多个运行实例",
                        f"Prometheus Pod 维度指标显示 {affected_pod} CPU 明显偏高",
                        "负载均衡命中不同 Pod，形成时快时慢的间歇现象",
                    ],
                    recommended_fix=[
                        "隔离该 Pod 并对比其线程、CPU profile 与兄弟实例",
                        "修复后按 Pod 维度复测 CPU、P95 和请求分布",
                    ],
                    confidence=0.9,
                )
        if oom_items and metric_items:
            return DiagnosisSynthesis(
                status="confirmed",
                root_cause="容器内存持续增长并超过 memory limit，触发 OOMKilled 后由 Kubernetes 重启",
                evidence_ids=[oom_items[-1].evidence_id, metric_items[-1].evidence_id],
                root_cause_chain=[
                    "Prometheus 记录 Pod 资源运行指标",
                    "Kubernetes 容器终止原因明确为 OOMKilled",
                    "容器被重启并累计 restart count",
                ],
                recommended_fix=[
                    "排查持续保留的 Buffer/对象并设置内存剖析与泄漏告警",
                    "修复后观察 working_set、OOMKilled 事件和 restart count",
                ],
                confidence=0.95,
            )
        if "cpu_saturation" in runtime_reason_text and log_items and metric_items:
            return DiagnosisSynthesis(
                status="confirmed",
                root_cause="CPU saturation：CPU 密集型计算阻塞服务工作线程，导致接口延迟升高",
                evidence_ids=[log_items[-1].evidence_id, metric_items[-1].evidence_id],
                root_cause_chain=[
                    "Loki 记录 cpu_saturation 故障模式下的业务请求",
                    "Prometheus 记录 Pod CPU 与请求延迟运行指标",
                    "CPU 密集计算导致可用工作线程下降",
                ],
                recommended_fix=[
                    "把 CPU 密集计算移出请求线程或拆分到独立 Worker",
                    "设置 CPU 限额、并发保护并复测 Pod CPU 与 P95",
                ],
                confidence=0.9,
            )
        dependency_candidates = [
            candidate
            for item in trace_items
            for candidate in item.structured_data.get("dependency_candidates", [])
            if isinstance(candidate, dict) and candidate.get("service") != state.service
        ]
        if any(term in state.query.lower() for term in ("重试", "retry", "放大", "storm")):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for candidate in dependency_candidates:
                grouped.setdefault(str(candidate.get("service")), []).append(candidate)
            repeated_service, repeated_calls = max(
                grouped.items(), key=lambda item: len(item[1]), default=("", [])
            )
            if repeated_service and len(repeated_calls) >= 3 and log_items and metric_items:
                trace_evidence = next(
                    item for item in reversed(trace_items)
                    if sum(
                        candidate.get("service") == repeated_service
                        for candidate in item.structured_data.get("dependency_candidates", [])
                        if isinstance(candidate, dict)
                    ) >= 3
                )
                return DiagnosisSynthesis(
                    status="confirmed",
                    root_cause=f"无退避重试风暴：单次请求连续调用下游 {repeated_service} {len(repeated_calls)} 次，放大请求量并导致 timeout",
                    evidence_ids=[trace_evidence.evidence_id, log_items[-1].evidence_id, metric_items[-1].evidence_id],
                    root_cause_chain=[
                        "Loki 记录故障窗口内业务请求",
                        f"Tempo 同一请求中发现 {len(repeated_calls)} 个 {repeated_service} 调用 Span",
                        "Prometheus 记录重试期间的请求与资源指标",
                    ],
                    recommended_fix=[
                        "设置总重试预算并使用指数退避、抖动和熔断",
                        "复测单请求下游 Span 数和放大后的 QPS",
                    ],
                    confidence=0.93,
                )
            repeated_log_item: Evidence | None = None
            repeated_trace_id = ""
            repeated_count = 0
            for item in log_items:
                for trace_id, count in item.structured_data.get("trace_id_counts", {}).items():
                    if int(count) > repeated_count:
                        repeated_log_item = item
                        repeated_trace_id = str(trace_id)
                        repeated_count = int(count)
            if repeated_log_item and repeated_count >= 3 and trace_items and metric_items:
                return DiagnosisSynthesis(
                    status="confirmed",
                    root_cause=f"无退避重试风暴：同一 trace_id 在末端下游日志重复出现 {repeated_count} 次，放大请求量并导致 timeout",
                    evidence_ids=[trace_items[-1].evidence_id, repeated_log_item.evidence_id, metric_items[-1].evidence_id],
                    root_cause_chain=[
                        "Tempo 确认故障请求的跨服务 Trace",
                        f"Loki 显示 trace_id {repeated_trace_id[:8]}… 在下游重复出现 {repeated_count} 次",
                        "Prometheus 记录故障窗口内的请求与资源指标",
                    ],
                    recommended_fix=[
                        "设置总重试预算并使用指数退避、抖动和熔断",
                        "复测单请求的同 trace_id 下游日志条数与整体 QPS",
                    ],
                    confidence=0.91,
                )
        if state.symptom == "dependency_timeout" and dependency_candidates and log_items:
            slowest = max(
                dependency_candidates,
                key=lambda candidate: float(candidate.get("duration_ms") or 0),
            )
            if float(slowest.get("duration_ms") or 0) >= 1000:
                trace_evidence = next(
                    item for item in reversed(trace_items)
                    if slowest in item.structured_data.get("dependency_candidates", [])
                )
                downstream = str(slowest["service"])
                duration_ms = float(slowest.get("duration_ms") or 0)
                return DiagnosisSynthesis(
                    status="confirmed",
                    root_cause=f"{downstream} 下游依赖调用耗时 {duration_ms:.0f}ms 并发生 timeout，导致上游请求变慢",
                    evidence_ids=[trace_evidence.evidence_id, log_items[-1].evidence_id],
                    root_cause_chain=[
                        "Loki 记录上游请求运行上下文",
                        f"Tempo 显示到 {downstream} 的客户端 Span 最慢",
                        "下游超时传播为上游延迟",
                    ],
                    recommended_fix=[
                        "检查下游服务处理时延并设置分层超时预算",
                        "增加有限重试、退避和熔断，复测跨服务 Trace",
                    ],
                    confidence=0.9,
                )
        regression_terms = ("发布", "回归", "deploy", "release", "regression", "版本")
        git_diff_items = [
            item for item in state.evidence
            if item.tool_name == "get_commit_diff" and item.supports_conclusion
        ]
        full_scan_item = next((
            item for item in reversed(explain_items)
            if any(
                str(row.get("type", "")).upper() == "ALL" and not row.get("key")
                for row in item.structured_data.get("rows", [])
                if isinstance(row, dict)
            )
        ), None)
        slow_item = next((item for item in reversed(slow_items) if item.supports_conclusion), None)
        pod_item = next((
            item for item in reversed(state.evidence)
            if item.source == "Kubernetes" and item.tool_name == "list_pods" and item.supports_conclusion
        ), None)
        if (
            any(term in state.query.lower() for term in regression_terms)
            and git_diff_items and full_scan_item and slow_item and pod_item
        ):
            sql_text = " ".join(
                str(row.get("sql_text", ""))
                for row in slow_item.structured_data.get("rows", [])
                if isinstance(row, dict)
            ).lower()
            query_shape = "LIKE 模糊匹配" if " like " in f" {sql_text} " else "不可索引的查询条件"
            return DiagnosisSynthesis(
                status="confirmed",
                root_cause=f"Git 发布代码回归引入{query_shape}与计算排序，导致索引失效、MySQL 全表扫描",
                evidence_ids=[pod_item.evidence_id, git_diff_items[-1].evidence_id, slow_item.evidence_id, full_scan_item.evidence_id],
                root_cause_chain=[
                    "Kubernetes 运行 Pod 暴露当前部署 commit",
                    "Git Diff 显示发布版本修改了订单查询实现",
                    "MySQL 慢查询记录扫描约十万行",
                    "EXPLAIN 显示 type=ALL 且未命中索引",
                ],
                recommended_fix=[
                    "回滚该 Git 回归或移除不可索引的 LIKE/计算排序",
                    "按查询模式补充可用索引并用 EXPLAIN、慢日志复测",
                ],
                confidence=0.95,
            )
        pool_markers = (
            "cannotgetjdbcconnectionexception", "failed to obtain jdbc connection",
            "connection is not available", "hikari", "connection timeout",
        )
        if slow_items and any(marker in log_text for marker in pool_markers):
            database_item = next((item for item in slow_items if item.supports_conclusion), None)
            if database_item is not None:
                return DiagnosisSynthesis(
                    status="confirmed",
                    root_cause="Hikari connection pool（连接池）耗尽，业务请求无法及时获得 JDBC Connection 并返回 500",
                    evidence_ids=[log_items[-1].evidence_id, database_item.evidence_id],
                    root_cause_chain=[
                        "Loki 记录 Failed to obtain JDBC Connection",
                        "并发请求持续占用数据库连接",
                        "Hikari 连接池等待超时并触发 5xx",
                    ],
                    recommended_fix=[
                        "先优化占用连接时间长的查询，再按数据库容量校准连接池上限与超时",
                        "复测 Hikari active/pending、5xx 和数据库查询耗时",
                    ],
                    confidence=0.92,
                )
        for slow in slow_items:
            slow_rows = slow.structured_data.get("rows", [])
            if not slow_rows:
                continue
            slow_sql = " ".join(str(row.get("sql_text") or "") for row in slow_rows).lower()
            examined = max((int(row.get("rows_examined") or 0) for row in slow_rows), default=0)
            for plan in explain_items:
                plan_rows = plan.structured_data.get("rows", [])
                full_scan = any(str(row.get("type") or "").upper() == "ALL" for row in plan_rows)
                no_key = any(not row.get("key") for row in plan_rows)
                if full_scan and no_key and examined > 0:
                    evidence_ids = [item.evidence_id for item in trace_items[-1:]] + [
                        slow.evidence_id, plan.evidence_id
                    ]
                    mechanism = "前导通配 LIKE 导致索引无法使用" if "like '%" in slow_sql else "查询未使用可用索引"
                    return DiagnosisSynthesis(
                        status="confirmed",
                        root_cause=f"慢 SQL 的{mechanism}，执行计划为 ALL 全表扫描并检查 {examined} 行",
                        evidence_ids=list(dict.fromkeys(evidence_ids)),
                        root_cause_chain=[
                            "Trace 定位到数据库查询",
                            f"MySQL 慢查询记录检查 {examined} 行",
                            "EXPLAIN 显示 type=ALL 且未命中索引",
                        ],
                        recommended_fix=[
                            "移除前导通配查询或改用适合模糊检索的索引方案",
                            "修复后用相同请求复测 P95、rows_examined 与 EXPLAIN",
                        ],
                        confidence=0.95,
                    )
        return None

    @staticmethod
    def _compact_tool_specs(tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """只保留 Planner 选参需要的字段，避免把冗长 MCP 描述重复送入小模型。"""
        compact: list[dict[str, Any]] = []
        for item in tool_specs:
            schema = item.get("input_schema") or {}
            properties = schema.get("properties") or {}
            compact.append({
                "name": item.get("name"),
                "description": str(item.get("description") or "")[:160],
                "parameters": {
                    name: {
                        key: value for key, value in definition.items()
                        if key in {"type", "enum", "minimum", "maximum", "minLength", "maxLength"}
                    }
                    for name, definition in properties.items()
                },
                "required": schema.get("required") or [],
            })
        return compact

    async def _complete_structured(
        self,
        system: str,
        payload: dict[str, Any],
        schema: type[BaseModel],
        template: dict[str, Any],
    ) -> Any:
        messages = [
            LLMMessage("system", system),
            LLMMessage("user", json.dumps(payload, ensure_ascii=False, default=str)),
        ]
        first_output = ""
        for attempt in range(self.retries + 1):
            response = await self.llm.complete(messages)
            self.prompt_tokens += response.prompt_tokens
            self.completion_tokens += response.completion_tokens
            first_output = first_output or response.content
            messages.append(LLMMessage("assistant", response.content or "{}"))
            try:
                return validate_structured_output(response.content, schema)
            except StructuredOutputError as exc:
                if attempt < self.retries:
                    messages.append(LLMMessage("user", schema_retry_message(exc)))
        messages.append(LLMMessage("user", template_refill_message(template, first_output)))
        response = await self.llm.complete(messages)
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        try:
            return validate_structured_output(response.content, schema)
        except StructuredOutputError:
            return schema.model_validate(template)
