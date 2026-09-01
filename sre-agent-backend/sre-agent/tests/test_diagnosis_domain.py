"""不依赖外部观测系统的 Diagnosis 领域契约测试。"""

import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_user
from app.diagnosis.models import DiagnosisSession
from app.diagnosis.orchestrator import DiagnosisOrchestrator
from app.diagnosis.router import get_diagnosis_repository, router as diagnosis_router
from app.diagnosis.schemas import DiagnosisCreateRequest
from app.workflow.models import DiagnosisReport, Evidence


class _Catalog:
    services = {
        "order-service": {"dependencies": ["payment-service"], "language": "Java"},
        "payment-service": {"dependencies": [], "language": "Node.js"},
    }

    @staticmethod
    def resolve(query: str) -> str:
        return "order-service" if "order" in query else "unknown"


class _Workflow:
    catalog = _Catalog()


def _report() -> DiagnosisReport:
    evidence = Evidence(
        source="TEMPO", source_type="TEMPO", tool_name="query_trace", title="Trace",
        detail="payment span slow", summary="payment span slow",
        structured_data={
            "services": ["payment-service"],
            "dependency_candidates": [{"service": "payment-service", "duration_ms": 820.5}],
        },
        timestamp=datetime.now(timezone.utc), evidence_id="ev-trace",
    )
    return DiagnosisReport(
        query="order timeout", run_id="run-1", service="order-service",
        symptom="latency", environment="test", time_range="最近 30 分钟",
        conclusion="证据不足：payment-service latency", status="insufficient_evidence",
        decision_summary="payment-service 下游延迟异常", root_cause="payment-service latency",
        evidence=[evidence], root_cause_chain=["order-service", "payment-service"],
        recommended_fix=["补充日志证据"], confidence=0.45, candidates=[],
        investigation_timeline=[], workflow_phases=[], context_compaction={},
    )


class DiagnosisDomainTest(unittest.TestCase):
    def test_module_owned_mysql_schema_has_comments(self) -> None:
        sql = (Path(__file__).parents[1] / "app" / "diagnosis" / "sql" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("COMMENT='事件级 Diagnosis Session'", sql)
        self.assertIn("COMMENT 'Evidence 唯一标识", sql)
        self.assertIn("diagnosis_graph_edges", sql)
        self.assertIn("diagnosis_events", sql)

    def test_trigger_target_contract(self) -> None:
        DiagnosisCreateRequest(trigger_type="QUESTION", question="最近订单为什么超时")
        with self.assertRaises(ValidationError):
            DiagnosisCreateRequest(
                trigger_type="QUESTION", question="为什么超时",
                initial_target={"type": "SERVICE", "name": "order-service"},
            )

    def test_graph_and_root_cause_are_backend_derived(self) -> None:
        orchestrator = DiagnosisOrchestrator(_Workflow(), None, None)  # type: ignore[arg-type]
        request = DiagnosisCreateRequest(
            trigger_type="SERVICE", question="order timeout",
            initial_target={"type": "SERVICE", "name": "order-service"},
        )
        report = _report()
        graph, affected = orchestrator._build_graph(report, request)
        root = orchestrator._build_root_cause(report, graph)

        self.assertEqual(affected, ["order-service", "payment-service"])
        self.assertTrue(any(edge.relation == "HTTP" and edge.latency_ms == 820.5 for edge in graph.edges))
        self.assertEqual(root.root_resource.name, "payment-service")
        self.assertTrue(any(node.name == "payment-service" and node.status == "ROOT_CAUSE" for node in graph.nodes))

    def test_quick_result_is_complete_without_persistence(self) -> None:
        orchestrator = DiagnosisOrchestrator(_Workflow(), None, None)  # type: ignore[arg-type]
        request = DiagnosisCreateRequest(
            trigger_type="SERVICE", question="order timeout",
            initial_target={"type": "SERVICE", "name": "order-service"},
        )

        result = orchestrator.build_quick_result(_report(), request)

        self.assertEqual(result["affected_services"], ["order-service", "payment-service"])
        self.assertEqual(result["root_cause"]["root_resource"]["name"], "payment-service")
        self.assertEqual(result["report"]["root_cause_chain"], ["order-service", "payment-service"])

    def test_history_endpoint_uses_session_contract(self) -> None:
        session = DiagnosisSession(
            id="d" * 32, conversation_id="c" * 32, question="order timeout",
            trigger_type="QUESTION", status="COMPLETED",
            affected_services=["order-service", "payment-service"],
            created_at="2026-09-01T00:00:00+00:00", updated_at="2026-09-01T00:01:00+00:00",
        )

        class _Repository:
            @staticmethod
            def list_for_user(user_id: str, limit: int):
                self.assertEqual(user_id, "user-1")
                self.assertEqual(limit, 50)
                return [session]

        application = FastAPI()
        application.include_router(diagnosis_router)
        application.dependency_overrides[require_user] = lambda: {"id": "user-1", "username": "tester"}
        application.dependency_overrides[get_diagnosis_repository] = _Repository
        response = TestClient(application).get("/api/diagnoses")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["affected_services"], ["order-service", "payment-service"])


if __name__ == "__main__":
    unittest.main()
