"""Intent Router 的结构化容错、分流与工具闸门测试。"""

import asyncio
from pathlib import Path

from app.intent import IntentRouter, IntentWorkflowRouter, SREIntent
from app.llm import LLMResponse
from app.workflow import DiagnosisWorkflow
from app.workflow.models import WorkflowPhase


class StubLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.messages: list[list] = []

    async def complete(self, messages):
        self.messages.append(messages.copy())
        return LLMResponse(next(self.responses), "intent-stub", "stub")


class RecordingWorkflow:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, message, on_event=None, **kwargs):
        self.calls.append({"message": message, **kwargs})
        return object()


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {"source": name, "data": []}


def test_intent_router_classifies_specific_incident() -> None:
    llm = StubLLM([
        '{"intent":"SPECIFIC_INCIDENT","target":"order-service","symptom":"high_latency"}'
    ])

    decision = asyncio.run(IntentRouter(llm).classify("订单接口延迟很高"))

    assert decision.intent is SREIntent.SPECIFIC_INCIDENT
    assert decision.target == "order-service"
    assert decision.symptom == "high_latency"


def test_explicit_service_incident_uses_deterministic_fast_path() -> None:
    llm = StubLLM([])

    decision = asyncio.run(IntentRouter(llm).classify("inventory-service 接口延迟很高"))

    assert decision.intent is SREIntent.SPECIFIC_INCIDENT
    assert decision.target == "inventory-service"
    assert decision.symptom == "latency"
    assert llm.messages == []


def test_catalog_short_service_name_uses_deterministic_fast_path() -> None:
    llm = StubLLM([])
    router = IntentRouter(llm, service_names=["order-service", "payment-service"])
    decision = asyncio.run(router.classify("order 接口突然大量报 500"))
    assert decision.target == "order-service"
    assert decision.symptom == "5xx"
    assert llm.messages == []


def test_catalog_chinese_alias_uses_deterministic_fast_path() -> None:
    llm = StubLLM([])
    router = IntentRouter(llm, service_aliases={"用户": "user-service"})
    decision = asyncio.run(router.classify("用户模块响应变慢，CPU 是否打满？"))
    assert decision.target == "user-service"
    assert decision.symptom == "latency"
    assert llm.messages == []


def test_intent_router_repairs_json_before_retry() -> None:
    llm = StubLLM([
        '结果：{"intent":"GENERAL_DIAGNOSIS","target":null,"symptom":"system_health",}'
    ])

    decision = asyncio.run(IntentRouter(llm).classify("帮我巡检整个系统"))

    assert decision.intent is SREIntent.GENERAL_DIAGNOSIS
    assert len(llm.messages) == 1


def test_incomplete_specific_incident_must_be_corrected_before_routing() -> None:
    llm = StubLLM([
        '{"intent":"SPECIFIC_INCIDENT","target":null,"symptom":"latency"}',
        '{"intent":"NEED_CLARIFICATION","target":null,"symptom":null}',
    ])

    decision = asyncio.run(IntentRouter(llm).classify("好像有点慢"))

    assert decision.intent is SREIntent.NEED_CLARIFICATION
    assert "schema_validation" in llm.messages[1][-1].content


def test_intent_router_uses_template_after_three_retries() -> None:
    llm = StubLLM([
        "bad-initial",
        "bad-retry-1",
        "bad-retry-2",
        "bad-retry-3",
        '{"intent":"NEED_CLARIFICATION","target":null,"symptom":null}',
    ])

    decision = asyncio.run(IntentRouter(llm).classify("有问题"))

    assert decision.intent is SREIntent.NEED_CLARIFICATION
    assert "structured_output_template_refill" in llm.messages[4][-1].content


def test_out_of_scope_never_enters_diagnosis_workflow() -> None:
    llm = StubLLM([
        '{"intent":"OUT_OF_SCOPE","target":null,"symptom":null}'
    ])
    workflow = RecordingWorkflow()
    router = IntentWorkflowRouter(IntentRouter(llm), workflow)  # type: ignore[arg-type]

    reply = asyncio.run(router.dispatch("帮我写一首诗"))

    assert reply.intent is SREIntent.OUT_OF_SCOPE
    assert workflow.calls == []


def test_specific_and_general_intents_route_with_explicit_scope() -> None:
    llm = StubLLM([
        '{"intent":"SPECIFIC_INCIDENT","target":"payment-service","symptom":"pod_restart"}',
        '{"intent":"GENERAL_DIAGNOSIS","target":null,"symptom":"system_health"}',
    ])
    workflow = RecordingWorkflow()
    router = IntentWorkflowRouter(IntentRouter(llm), workflow)  # type: ignore[arg-type]

    asyncio.run(router.dispatch("payment 一直重启"))
    asyncio.run(router.dispatch("巡检整个系统"))

    assert workflow.calls[0]["target"] == "payment-service"
    assert workflow.calls[0]["system_scan"] is False
    assert workflow.calls[1]["system_scan"] is True


def test_optional_multi_service_scope_is_forwarded_as_seed_set() -> None:
    llm = StubLLM([
        '{"intent":"GENERAL_DIAGNOSIS","target":null,"symptom":"timeout"}',
    ])
    workflow = RecordingWorkflow()
    router = IntentWorkflowRouter(IntentRouter(llm), workflow)  # type: ignore[arg-type]

    asyncio.run(router.dispatch(
        "最近请求大量超时",
        selected_services=["order-service", "payment-service"],
    ))

    assert workflow.calls[0]["target"] == "order-service"
    assert workflow.calls[0]["selected_services"] == ["order-service", "payment-service"]


def test_general_diagnosis_uses_global_scan_without_order_fallback() -> None:
    tools = RecordingTools()
    catalog = (
        Path(__file__).resolve().parents[3]
        / "sre-broken-system"
        / "sre-lab-infra"
        / "service-catalog.yaml"
    )
    workflow = DiagnosisWorkflow(tools, str(catalog), llm=None)  # type: ignore[arg-type]

    report = asyncio.run(workflow.run("巡检整个系统", system_scan=True, symptom="system_health"))

    assert WorkflowPhase.SYSTEM_SCAN in report.workflow_phases
    assert report.service == "unknown"
    assert not any(arguments.get("service") == "order-service" for _, arguments in tools.calls)
