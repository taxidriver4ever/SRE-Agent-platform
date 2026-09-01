"""P0：去场景硬编码、统一 Tool Result 与 Evidence Gate 验收。"""

from datetime import datetime, timezone
from pathlib import Path
import json

from app.evidence import normalize_tool_result
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.planner import EvidencePlanner
from app.workflow.models import DiagnosisState, DiagnosisSynthesis, Evidence, ToolCallRecord
from evals.run_evals import aggregate, validate_case


def test_workflow_contains_no_eval_or_fault_answer_shortcuts():
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in ("app/workflow/diagnosis.py", "app/workflow/planner.py")
    ).lower()
    forbidden = (
        "slow.example.com",
        "mode=slow_sql",
        "select count(*) from orders",
        "sre-001",
        "sre-010",
        "buffer 导致",
        "低效质数",
    )
    assert all(value not in source for value in forbidden)


def test_unified_tool_result_extracts_arbitrary_trace_and_sql():
    trace_id = "0123456789abcdef0123456789abcdef"
    sql = "SELECT sku, quantity FROM warehouse_items WHERE region = 'north-9'"
    result = normalize_tool_result(
        "query_trace",
        {"trace_id": trace_id},
        {
            "resourceSpans": [{
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "warehouse-v2"}},
                    {"key": "db.statement", "value": {"stringValue": sql}},
                ],
                "traceID": trace_id,
            }],
        },
        [],
    )
    assert result.tool == "query_trace"
    assert result.status == "success"
    assert result.structured_data["trace_ids"] == [trace_id]
    assert result.structured_data["sql_statements"] == [sql]
    assert result.structured_data["services"] == ["warehouse-v2"]
    assert any("EXPLAIN" in hint for hint in result.next_hints)


def test_unified_mysql_result_preserves_bounded_rows_for_evidence_synthesis():
    result = normalize_tool_result(
        "explain_sql",
        {"sql": "SELECT * FROM inventory"},
        {"data": {"row_count": 1, "rows": [{"table": "inventory", "type": "ALL", "key": None}]}},
        [],
    )
    assert result.structured_data["row_count"] == 1
    assert result.structured_data["rows"][0]["type"] == "ALL"


def test_unified_log_result_preserves_runtime_error_messages_beyond_summary():
    result = normalize_tool_result(
        "query_logs",
        {"service": "orders"},
        {"data": {"padding": "x" * 2000, "records": [{"message": "Failed to obtain JDBC Connection"}]}},
        [],
        summary_limit=100,
    )
    assert "Failed to obtain JDBC Connection" in result.structured_data["runtime_messages"]


def test_unified_log_result_preserves_fault_mode():
    result = normalize_tool_result(
        "query_logs", {"service": "users"},
        {"records": [{"message": "profile read", "fault_mode": "cpu_saturation"}]}, [],
    )
    assert result.structured_data["runtime_reasons"] == ["cpu_saturation"]


def test_unified_log_result_preserves_repeated_trace_id_counts():
    trace_id = "1" * 32
    result = normalize_tool_result(
        "query_logs", {"service": "downstream"},
        {"records": [{"trace_id": trace_id}, {"trace_id": trace_id}, {"trace_id": trace_id}]}, [],
    )
    assert result.structured_data["trace_ids"] == [trace_id]
    assert result.structured_data["trace_id_counts"][trace_id] == 3


def test_unified_trace_extracts_downstream_service_and_duration():
    result = normalize_tool_result(
        "query_trace", {"trace_id": "a" * 32},
        {"startTimeUnixNano": "1000000000", "endTimeUnixNano": "3500000000", "attributes": [
            {"key": "url.full", "value": {"stringValue": "http://inventory-service:8081/inventory/SKU-1"}}
        ]}, [],
    )
    assert result.structured_data["services"] == ["inventory-service"]
    assert result.structured_data["dependency_candidates"][0]["duration_ms"] == 2500.0


def test_unified_prometheus_result_preserves_labeled_numeric_samples():
    result = normalize_tool_result(
        "query_metrics", {"query": "cpu"},
        {"data": {"result": {"result": [{"metric": {"pod": "orders-a"}, "value": [1, "0.25"]}]}}}, [],
    )
    assert result.structured_data["metric_samples"] == [{"labels": {"pod": "orders-a"}, "value": 0.25}]


def test_planner_expands_new_trace_search_after_historical_slowest_trace_was_read():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="排查重试风暴", service="order-service", symptom="latency")
    state.evidence = [
        Evidence(
            source="Tempo", tool_name="query_trace", title="订单 Trace 搜索", detail="search",
            timestamp=now, evidence_id="ev_order_search",
            structured_data={"trace_candidates": [{"trace_id": "a" * 32, "name": "POST /orders", "duration_ms": 9000}]},
        ),
        Evidence(
            source="Tempo", tool_name="query_trace", title="库存 Trace 搜索", detail="search",
            timestamp=now, evidence_id="ev_inventory_search",
            structured_data={"trace_candidates": [{"trace_id": "b" * 32, "name": "GET /inventory", "duration_ms": 2000}]},
        ),
    ]
    state.timeline = [
        ToolCallRecord(tool_name="query_trace", arguments={"trace_id": "a" * 32}, timestamp=now, duration_ms=10)
    ]

    decision = EvidencePlanner._evidence_driven_decision(state)

    assert decision is not None
    assert decision.tool_name == "query_trace"
    assert decision.arguments == {"trace_id": "b" * 32}
    assert decision.parent_evidence_ids == ["ev_inventory_search"]


def test_evidence_gate_downgrades_uncited_or_indirect_model_claim():
    workflow = object.__new__(DiagnosisWorkflow)
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="warehouse-v2 变慢", service="warehouse-v2")
    state.evidence = [
        Evidence(
            source="Git",
            tool_name="read_file_at_commit",
            title="源码",
            detail="source preview",
            timestamp=now,
            evidence_id="ev_git",
            direct_evidence=False,
        ),
        Evidence(
            source="Prometheus",
            tool_name="query_metrics",
            title="延迟",
            detail="p95 high",
            timestamp=now,
            evidence_id="ev_metric",
            direct_evidence=True,
        ),
    ]
    state.synthesis = DiagnosisSynthesis(
        status="confirmed",
        root_cause="某段源码导致数据库扫描",
        evidence_ids=["ev_git", "missing"],
        confidence=0.95,
    )
    report = workflow._report(state)
    assert report.status == "insufficient_evidence"
    assert report.findings == []
    assert report.confidence <= 0.49


def test_evidence_gate_keeps_traceable_confirmed_finding():
    workflow = object.__new__(DiagnosisWorkflow)
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="warehouse-v2 变慢", service="warehouse-v2")
    state.evidence = [
        Evidence(
            source="Tempo",
            tool_name="query_trace",
            title="慢 Trace",
            detail="database span 1800ms",
            timestamp=now,
            evidence_id="ev_trace",
            direct_evidence=True,
        ),
        Evidence(
            source="MySQL",
            tool_name="explain_sql",
            title="执行计划",
            detail="access_type ALL",
            timestamp=now,
            evidence_id="ev_plan",
            parent_evidence_ids=["ev_trace"],
            direct_evidence=True,
        ),
    ]
    state.synthesis = DiagnosisSynthesis(
        status="confirmed",
        root_cause="warehouse_items 查询发生全表扫描",
        evidence_ids=["ev_trace", "ev_plan"],
        root_cause_chain=["数据库 Span 变慢", "执行计划为 ALL"],
        confidence=0.88,
    )
    report = workflow._report(state)
    assert report.status == "confirmed"
    assert report.findings[0].evidence_ids == ["ev_trace", "ev_plan"]


def test_fixed_ten_eval_cases_have_non_leaking_contract():
    eval_dir = Path(__file__).resolve().parents[1] / "evals"
    files = [eval_dir / f"SRE-{index:03d}.json" for index in range(1, 11)]
    assert all(path.exists() for path in files)
    for path in files:
        case = json.loads(path.read_text(encoding="utf-8"))
        validate_case(case, path)
        agent_payload = {"message": case["symptom"], "project_id": "sre-lab"}
        serialized = json.dumps(agent_payload, ensure_ascii=False)
        assert case["case_id"] not in serialized
        assert "expected_root_cause" not in serialized
        assert "required_evidence" not in serialized


def test_eval_aggregate_keeps_failures_visible():
    metrics = aggregate([
        {"passed": True, "service_correct": True, "root_cause_correct": True, "evidence_status": "COMPLETE", "tool_calls": 4, "diagnosis_time_ms": 100, "token_usage": 20},
        {"passed": False, "service_correct": True, "root_cause_correct": False, "evidence_status": "PARTIAL", "tool_calls": 6, "diagnosis_time_ms": 300, "token_usage": 40},
    ])
    assert metrics["cases"] == 2
    assert metrics["passed"] == 1
    assert metrics["root_cause_accuracy"] == 0.5
    assert metrics["evidence_completion_rate"] == 0.5


def test_planner_never_explains_unbound_parameterized_sql():
    assert EvidencePlanner._is_explainable_sql("SELECT * FROM orders WHERE id = ?") is False
    assert EvidencePlanner._is_explainable_sql("SELECT * FROM orders WHERE id = $1") is False
    assert EvidencePlanner._is_explainable_sql("SELECT * FROM orders WHERE id = :order_id") is False
    assert EvidencePlanner._is_explainable_sql("SELECT * FROM orders WHERE id = 42") is True


def test_structured_slow_query_and_full_scan_form_deterministic_synthesis():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="某接口变慢", service="unknown-service", symptom="latency")
    state.evidence = [
        Evidence(source="Tempo", tool_name="query_trace", title="Trace", detail="db span", timestamp=now,
                 evidence_id="ev_trace", direct_evidence=True, supports_conclusion=True),
        Evidence(source="MySQL", tool_name="query_slow_queries", title="慢查询", detail="slow", timestamp=now,
                 evidence_id="ev_slow", direct_evidence=True, supports_conclusion=True,
                 structured_data={"rows": [{"rows_examined": 100028, "sql_text": "SELECT * FROM t WHERE name LIKE '%x%'"}]}),
        Evidence(source="MySQL", tool_name="explain_sql", title="执行计划", detail="ALL", timestamp=now,
                 evidence_id="ev_plan", direct_evidence=True, supports_conclusion=True,
                 structured_data={"rows": [{"type": "ALL", "key": None}]}),
    ]

    synthesis = EvidencePlanner._deterministic_synthesis(state)

    assert synthesis is not None
    assert synthesis.status == "confirmed"
    assert synthesis.evidence_ids == ["ev_trace", "ev_slow", "ev_plan"]
    assert "全表扫描" in synthesis.root_cause


def test_connection_pool_logs_and_mysql_evidence_form_deterministic_synthesis():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="接口大量 500", service="unknown-service", symptom="5xx")
    state.evidence = [
        Evidence(source="Loki", tool_name="query_logs", title="错误日志",
                 detail="CannotGetJdbcConnectionException: Failed to obtain JDBC Connection",
                 timestamp=now, evidence_id="ev_log", direct_evidence=True, supports_conclusion=True),
        Evidence(source="MySQL", tool_name="query_slow_queries", title="数据库运行记录",
                 detail="row_count=1", timestamp=now, evidence_id="ev_mysql",
                 direct_evidence=True, supports_conclusion=True,
                 structured_data={"rows": [{"query_time": "PT1.2S"}]}),
    ]
    synthesis = EvidencePlanner._deterministic_synthesis(state)
    assert synthesis is not None
    assert synthesis.status == "confirmed"
    assert synthesis.evidence_ids == ["ev_log", "ev_mysql"]
    assert "连接池" in synthesis.root_cause


def test_release_diff_and_full_scan_form_regression_synthesis_before_incidental_pool_log():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="发布后变慢，确认代码回归", service="order-service", symptom="latency")
    state.evidence = [
        Evidence(source="Kubernetes", tool_name="list_pods", title="Pods", detail="commit=abc1234",
                 timestamp=now, evidence_id="ev_pod", direct_evidence=True),
        Evidence(source="Loki", tool_name="query_logs", title="Logs", detail="Hikari warning",
                 timestamp=now, evidence_id="ev_log", direct_evidence=True),
        Evidence(source="MySQL", tool_name="query_slow_queries", title="Slow", detail="rows",
                 timestamp=now, evidence_id="ev_slow", direct_evidence=True,
                 structured_data={"rows": [{"rows_examined": 100000, "sql_text": "SELECT * FROM orders WHERE email LIKE '%x%'"}]}),
        Evidence(source="MySQL", tool_name="explain_sql", title="Plan", detail="ALL",
                 timestamp=now, evidence_id="ev_plan", direct_evidence=True,
                 structured_data={"rows": [{"type": "ALL", "key": None}]}),
        Evidence(source="Git", tool_name="get_commit_diff", title="Diff", detail="query changed",
                 timestamp=now, evidence_id="ev_diff", direct_evidence=True),
    ]
    synthesis = EvidencePlanner._deterministic_synthesis(state)
    assert synthesis is not None
    assert "Git" in synthesis.root_cause
    assert "回归" in synthesis.root_cause
    assert "LIKE" in synthesis.root_cause


def test_oom_kubernetes_and_metrics_form_deterministic_synthesis():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="服务内存异常并重启", service="unknown-service", symptom="pod_restart")
    state.evidence = [
        Evidence(source="Kubernetes", tool_name="get_pod", title="Pod", detail="OOMKilled",
                 timestamp=now, evidence_id="ev_pod", direct_evidence=True, supports_conclusion=True,
                 structured_data={"runtime_reasons": ["OOMKilled", "3"]}),
        Evidence(source="Prometheus", tool_name="query_metrics", title="资源指标", detail="memory",
                 timestamp=now, evidence_id="ev_metric", direct_evidence=True, supports_conclusion=True),
    ]
    synthesis = EvidencePlanner._deterministic_synthesis(state)
    assert synthesis is not None
    assert synthesis.status == "confirmed"
    assert "OOMKilled" in synthesis.root_cause


def test_invalid_liveness_path_and_events_form_probe_restart_synthesis():
    now = datetime.now(timezone.utc)
    state = DiagnosisState(query="服务不断重启", service="orders", symptom="pod_restart")
    state.evidence = [
        Evidence(source="Kubernetes", tool_name="get_pod", title="Pod", detail="probe",
                 timestamp=now, evidence_id="ev_pod", direct_evidence=True,
                 structured_data={"probe_paths": ["/broken-health", "/actuator/health/readiness"]}),
        Evidence(source="Kubernetes", tool_name="get_pod_events", title="Events", detail="failed",
                 timestamp=now, evidence_id="ev_event", direct_evidence=True,
                 structured_data={"runtime_messages": ["Liveness probe failed: HTTP probe failed with statuscode: 500"]}),
        Evidence(source="Kubernetes", tool_name="get_restart_count", title="Restart", detail="1",
                 timestamp=now, evidence_id="ev_restart", direct_evidence=True,
                 structured_data={"runtime_reasons": ["1"]}),
    ]
    synthesis = EvidencePlanner._deterministic_synthesis(state)
    assert synthesis is not None
    assert "liveness" in synthesis.root_cause
    assert "重启" in synthesis.root_cause
