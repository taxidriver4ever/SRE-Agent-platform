"""拆分后的 Planner / Synthesis Rule 独立契约。"""

from datetime import datetime, timezone

from app.workflow.models import DiagnosisState, Evidence
from app.workflow.planning.decision_rules import evidence_driven_decision, is_explainable_sql
from app.workflow.planning.synthesis_rules import (
    deterministic_synthesis,
    resource_exhaustion_rule,
    slow_query_rule,
)


NOW = datetime.now(timezone.utc)


def evidence(tool: str, source: str, evidence_id: str, structured: dict) -> Evidence:
    return Evidence(
        source=source, tool_name=tool, title=tool, detail=tool, timestamp=NOW,
        evidence_id=evidence_id, structured_data=structured,
        direct_evidence=True, supports_conclusion=True,
    )


def test_trace_id_rule_chooses_exact_trace_from_evidence() -> None:
    state = DiagnosisState(query="请求失败", service="order-service", symptom="5xx")
    state.evidence = [evidence("query_logs", "Loki", "ev-log", {"trace_ids": ["a" * 32]})]

    decision = evidence_driven_decision(state)

    assert decision is not None
    assert decision.tool_name == "query_trace"
    assert decision.arguments == {"trace_id": "a" * 32}
    assert decision.parent_evidence_ids == ["ev-log"]


def test_sql_rule_rejects_mutation_and_unbound_placeholders() -> None:
    assert is_explainable_sql("SELECT * FROM orders WHERE id = 7")
    assert not is_explainable_sql("DELETE FROM orders")
    assert not is_explainable_sql("SELECT * FROM orders WHERE id = ?")


def test_oom_and_metrics_rule_confirms_resource_exhaustion() -> None:
    state = DiagnosisState(query="Pod 重启并且内存持续增长", service="payment-service", symptom="pod_restart")
    state.evidence = [
        evidence("get_pod", "Kubernetes", "ev-pod", {"runtime_reasons": ["OOMKilled"]}),
        evidence("query_metrics", "Prometheus", "ev-metric", {"metric_samples": [{"labels": {"pod": "p-1"}, "value": 1.0}]}),
    ]

    result = resource_exhaustion_rule(state)

    assert result is not None
    assert result.status == "confirmed"
    assert result.evidence_ids == ["ev-pod", "ev-metric"]


def test_slow_query_rule_requires_slow_record_and_full_scan_plan() -> None:
    state = DiagnosisState(query="查询变慢", service="order-service", symptom="latency")
    state.evidence = [
        evidence("query_slow_queries", "MySQL", "ev-slow", {"rows": [{"sql_text": "SELECT * FROM orders", "rows_examined": 1000}]}),
        evidence("explain_sql", "MySQL", "ev-plan", {"rows": [{"type": "ALL", "key": None}]}),
    ]

    result = slow_query_rule(state)

    assert result is not None
    assert result.status == "confirmed"
    assert result.evidence_ids == ["ev-slow", "ev-plan"]


def test_slow_query_rule_returns_none_when_evidence_is_incomplete() -> None:
    state = DiagnosisState(query="查询变慢", service="order-service", symptom="latency")
    state.evidence = [evidence("query_slow_queries", "MySQL", "ev-slow", {"rows": []})]

    assert slow_query_rule(state) is None


def test_full_scan_evidence_takes_precedence_over_secondary_pool_timeout_logs() -> None:
    state = DiagnosisState(query="查询变慢", service="order-service", symptom="latency")
    state.evidence = [
        evidence("query_logs", "Loki", "ev-log", {"runtime_messages": ["Failed to obtain JDBC Connection from Hikari"]}),
        evidence("query_slow_queries", "MySQL", "ev-slow", {
            "rows": [{"sql_text": "SELECT * FROM orders WHERE email LIKE '%example.com%'", "rows_examined": 100000}],
        }),
        evidence("explain_sql", "MySQL", "ev-plan", {"rows": [{"type": "ALL", "key": None}]}),
    ]

    result = deterministic_synthesis(state)

    assert result is not None
    assert "慢 SQL" in result.root_cause
    assert "全表扫描" in result.root_cause


def test_error_spike_with_pool_timeout_takes_precedence_over_secondary_full_scan() -> None:
    state = DiagnosisState(query="接口大量报 500", service="order-service", symptom="5xx")
    state.evidence = [
        evidence("query_logs", "Loki", "ev-log", {"runtime_messages": ["Failed to obtain JDBC Connection from Hikari"]}),
        evidence("query_slow_queries", "MySQL", "ev-slow", {
            "rows": [{"sql_text": "SELECT * FROM orders WHERE email LIKE '%example.com%'", "rows_examined": 100000}],
        }),
        evidence("explain_sql", "MySQL", "ev-plan", {"rows": [{"type": "ALL", "key": None}]}),
    ]

    result = deterministic_synthesis(state)

    assert result is not None
    assert "连接池" in result.root_cause
