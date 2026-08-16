"""只根据当前证据选择下一步调查动作并综合最终结论。"""

from __future__ import annotations

import json
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
        successful = {(item.tool_name, json.dumps(item.arguments, sort_keys=True, default=str)) for item in state.timeline if item.error is None}

        def called(tool: str, arguments: dict[str, Any] | None = None) -> bool:
            if arguments is None:
                return any(name == tool for name, _ in successful)
            return (tool, json.dumps(arguments, sort_keys=True, default=str)) in successful

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

        for trace_id in dict.fromkeys(trace_ids):
            arguments = {"trace_id": trace_id}
            if not called("query_trace", arguments):
                return PlannerDecision(
                    action="tool", tool_name="query_trace", arguments=arguments,
                    title="按日志关联 ID 精确读取 Trace",
                    reason="现有运行时证据包含 trace_id",
                    parent_evidence_ids=parents(lambda item: trace_id in item.structured_data.get("trace_ids", [])),
                )

        trace_candidates = [
            candidate
            for item in state.evidence if item.tool_name == "query_trace"
            for candidate in item.structured_data.get("trace_candidates", [])
            if isinstance(candidate, dict)
        ]
        non_health = [
            candidate for candidate in trace_candidates
            if not any(marker in str(candidate.get("name", "")).lower() for marker in ("health", "prometheus", "metrics"))
        ]
        if non_health:
            selected = max(non_health, key=lambda item: float(item.get("duration_ms") or 0))
            arguments = {"trace_id": selected["trace_id"]}
            if not called("query_trace", arguments):
                return PlannerDecision(
                    action="tool", tool_name="query_trace", arguments=arguments,
                    title="读取最慢业务 Trace 详情", reason="Trace 搜索结果包含非健康检查业务请求",
                    parent_evidence_ids=parents(lambda item: item.tool_name == "query_trace"),
                )

        for sql in dict.fromkeys(sql_statements):
            arguments = {"sql": sql}
            if not called("explain_sql", arguments):
                return PlannerDecision(
                    action="tool", tool_name="explain_sql", arguments=arguments,
                    title="验证运行时 SQL 执行计划",
                    reason="Trace 或数据库证据包含原始只读 SQL",
                    parent_evidence_ids=parents(lambda item: sql in item.structured_data.get("sql_statements", [])),
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

        if state.symptom in {"latency", "5xx"} and not called("query_slow_queries"):
            return PlannerDecision(
                action="tool", tool_name="query_slow_queries",
                arguments={"time_range_minutes": state.time_range_minutes, "limit": 10},
                title="检查近期数据库慢查询",
                reason="延迟或错误基线需要排除数据库等待",
                parent_evidence_ids=parents(lambda item: item.source in {"Prometheus", "Loki", "Tempo"}),
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

    async def synthesize(self, state: DiagnosisState) -> DiagnosisSynthesis:
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
