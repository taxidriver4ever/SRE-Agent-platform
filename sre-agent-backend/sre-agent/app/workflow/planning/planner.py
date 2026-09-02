"""Hybrid Evidence Planner：确定性规则优先，LLM 作为安全 fallback。"""

from typing import Any

from pydantic import BaseModel

from app.llm import LLM
from app.workflow.models import DiagnosisState, DiagnosisSynthesis
from app.workflow.planning.decision_rules import evidence_driven_decision, is_explainable_sql
from app.workflow.planning.models import PlannerDecision
from app.workflow.planning.structured import complete_structured
from app.workflow.planning.synthesis_rules import deterministic_synthesis


class EvidencePlanner:
    """LLM Planner 不接收 Eval Expected，只接收用户问题和真实 Tool Result 摘要。"""

    def __init__(self, llm: LLM, retries: int = 2) -> None:
        self.llm = llm
        self.retries = max(1, min(3, retries))
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.structured_output_retry_count = 0

    async def decide(self, state: DiagnosisState, tool_specs: list[dict[str, Any]]) -> PlannerDecision:
        deterministic = evidence_driven_decision(state)
        if deterministic is not None:
            return deterministic
        evidence = [{
            "evidence_id": item.evidence_id, "source": item.source, "tool": item.tool_name,
            "summary": item.detail, "structured_data": item.structured_data,
            "next_hints": item.next_hints, "supports_conclusion": item.supports_conclusion,
        } for item in state.evidence]
        calls = [{"tool": item.tool_name, "arguments": item.arguments, "success": item.error is None}
                 for item in state.timeline]
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
            "query": state.query, "service": state.service, "symptom": state.symptom,
            "repository": state.repository, "runtime_commit": state.runtime_commit,
            "source_code_location": state.source_code_location,
            "available_tools": self._compact_tool_specs(tool_specs), "evidence": evidence,
            "previous_calls": calls,
            "remaining_tool_calls": max(0, state.max_tool_steps - len(state.timeline)),
            "output_schema": {"action": "tool|finish|insufficient_evidence", "tool_name": "string|null",
                "arguments": {}, "title": "string", "reason": "string", "parent_evidence_ids": []},
        }
        template = PlannerDecision(action="insufficient_evidence", reason="证据不足").model_dump(mode="json")
        decision = await self._complete_structured(system, payload, PlannerDecision, template)
        known_ids = {item.evidence_id for item in state.evidence}
        decision.parent_evidence_ids = [item for item in decision.parent_evidence_ids if item in known_ids]
        return decision

    async def synthesize(self, state: DiagnosisState) -> DiagnosisSynthesis:
        deterministic = deterministic_synthesis(state)
        if deterministic is not None:
            return deterministic
        evidence = [{
            "evidence_id": item.evidence_id, "source": item.source, "tool": item.tool_name,
            "summary": item.detail, "structured_data": item.structured_data,
            "direct_evidence": item.direct_evidence, "supports_conclusion": item.supports_conclusion,
            "parent_evidence_ids": item.parent_evidence_ids,
            "references": [reference.model_dump(mode="json") for reference in item.source_references],
        } for item in state.evidence]
        system = (
            "你是证据约束的 SRE 诊断综合器。禁止使用预设故障答案，禁止从服务名猜根因。"
            "结论只能引用输入中的 evidence_id，并必须解释观测到的症状。"
            "confirmed 必须有直接运行时证据且引用至少两条互相支持的证据；只有代码、用户措辞、空结果或模型推测时必须返回 insufficient_evidence。"
            "证据互相矛盾时列入 contradictions 并返回 insufficient_evidence。"
            "修复建议只能针对已被证据支持的机制。只返回 JSON。/no_think"
        )
        payload = {
            "query": state.query, "service": state.service, "symptom": state.symptom, "evidence": evidence,
            "output_schema": {"status": "confirmed|insufficient_evidence", "root_cause": "string",
                "evidence_ids": [], "root_cause_chain": [], "recommended_fix": [],
                "confidence": 0.0, "contradictions": []},
            "valid_output_example": {"status": "insufficient_evidence", "root_cause": "证据不足，无法确认根因",
                "evidence_ids": [], "root_cause_chain": [], "recommended_fix": [],
                "confidence": 0.0, "contradictions": []},
        }
        template = DiagnosisSynthesis(status="insufficient_evidence", root_cause="证据不足，无法确认根因", confidence=0.0).model_dump(mode="json")
        return await self._complete_structured(system, payload, DiagnosisSynthesis, template)

    async def _complete_structured(
        self, system: str, payload: dict[str, Any], schema: type[BaseModel], template: dict[str, Any],
    ) -> Any:
        result, prompt_tokens, completion_tokens, retry_count = await complete_structured(
            self.llm, self.retries, system, payload, schema, template,
        )
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.structured_output_retry_count += retry_count
        return result

    @staticmethod
    def _compact_tool_specs(tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in tool_specs:
            schema = item.get("input_schema") or {}
            properties = schema.get("properties") or {}
            compact.append({
                "name": item.get("name"), "description": str(item.get("description") or "")[:160],
                "parameters": {name: {key: value for key, value in definition.items()
                    if key in {"type", "enum", "minimum", "maximum", "minLength", "maxLength"}}
                    for name, definition in properties.items()},
                "required": schema.get("required") or [],
            })
        return compact

    # 兼容现有测试与外部扩展；实现已移动到独立 Rule 模块。
    _evidence_driven_decision = staticmethod(evidence_driven_decision)
    _is_explainable_sql = staticmethod(is_explainable_sql)
    _deterministic_synthesis = staticmethod(deterministic_synthesis)
