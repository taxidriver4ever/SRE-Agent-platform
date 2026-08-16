"""P0：去场景硬编码、统一 Tool Result 与 Evidence Gate 验收。"""

from datetime import datetime, timezone
from pathlib import Path
import json

from app.evidence import normalize_tool_result
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.models import DiagnosisState, DiagnosisSynthesis, Evidence
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
