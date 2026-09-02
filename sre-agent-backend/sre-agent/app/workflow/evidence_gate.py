"""Evidence 分类与最终 confirmed 门槛。"""

from app.workflow.models import (
    DiagnosisFinding, DiagnosisReport, DiagnosisState, DiagnosisSynthesis,
)


def source_for_tool(tool_name: str) -> str:
    if tool_name.startswith("query_metric") or tool_name == "get_service_health":
        return "Prometheus"
    if tool_name == "query_logs":
        return "Loki"
    if tool_name == "query_trace":
        return "Tempo"
    if tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}:
        return "MySQL"
    if (tool_name in {"get_repository", "get_current_commit", "get_commit_diff", "list_changed_files"}
            or tool_name.startswith("get_commit") or tool_name.startswith("read_file")
            or tool_name.startswith("search_code")):
        return "Git"
    return "Kubernetes"


def supports_conclusion(tool_name: str, summary: str) -> bool:
    if tool_name.startswith("query_") or tool_name == "get_service_health":
        return not any(marker in summary for marker in ('"result": []', '"traces": []', '"row_count": 0'))
    return True


def is_direct_evidence(tool_name: str, arguments: dict, summary: str) -> bool:
    if not supports_conclusion(tool_name, summary):
        return False
    if tool_name == "query_trace":
        return bool(arguments.get("trace_id"))
    return tool_name in {
        "query_metrics", "get_service_health", "query_logs", "query_slow_queries",
        "query_sql_digest", "explain_sql", "get_pod", "get_pod_events",
        "get_restart_count", "get_deployment", "get_container_image",
    }


def build_report(state: DiagnosisState) -> DiagnosisReport:
    """引用、直接证据和 contradictions 全部通过后才允许 confirmed。"""
    synthesis = state.synthesis or DiagnosisSynthesis(
        status="insufficient_evidence", root_cause="证据不足，无法确认根因", confidence=0.0,
    )
    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    cited_ids = list(dict.fromkeys(item for item in synthesis.evidence_ids if item in evidence_by_id))
    cited = [evidence_by_id[item] for item in cited_ids]
    passed = (
        synthesis.status == "confirmed" and len(cited) >= 2
        and any(item.direct_evidence and item.supports_conclusion for item in cited)
        and all(item.supports_conclusion for item in cited) and not synthesis.contradictions
    )
    status = "confirmed" if passed else "insufficient_evidence"
    root = synthesis.root_cause.strip() or "证据不足，无法确认根因"
    confidence = synthesis.confidence if passed else min(synthesis.confidence, 0.49)
    conclusion = f"已确认：{root}" if passed else f"证据不足：{root}"
    findings = [DiagnosisFinding(finding=root, evidence_ids=cited_ids)] if passed else []
    return DiagnosisReport(
        query=state.query, run_id=state.run_id, service=state.service, affected_pod=state.pod_name,
        language=state.language, running_version=state.runtime_commit, git_sha=state.runtime_commit,
        source_code_location=state.source_code_location, repository_url=state.repository_url,
        symptom=state.symptom, environment=state.environment,
        time_range=f"最近 {state.time_range_minutes} 分钟", conclusion=conclusion,
        status=status, decision_summary=conclusion, root_cause=root, findings=findings,
        evidence=state.evidence,
        root_cause_chain=synthesis.root_cause_chain or ["当前观测证据", "尚不足以形成可验证根因"],
        recommended_fix=synthesis.recommended_fix or ["补充与候选机制直接相关的 Metrics、Logs、Trace 或运行时状态后重新诊断"],
        confidence=confidence, token_usage=state.prompt_tokens + state.completion_tokens,
        structured_output_retry_count=state.structured_output_retry_count,
        candidates=state.candidates, investigation_timeline=state.timeline, workflow_phases=state.phases,
        context_compaction={"strategy": "mysql-conversation-compaction-v1", "storage": "mysql", "stored_evidence": len(state.evidence)},
    )
