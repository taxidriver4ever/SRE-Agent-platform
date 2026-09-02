"""Baseline 并发、失败隔离与工具预算契约。"""

import asyncio
import time
from pathlib import Path

from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.models import DiagnosisState
from app.llm.gateway import GatewayRequestError


CATALOG = (
    Path(__file__).resolve().parents[3]
    / "sre-broken-system"
    / "sre-lab-infra"
    / "service-catalog.yaml"
)


class SlowBaselineTools:
    def __init__(self, *, fail_health: bool = False, delay: float = 0.08) -> None:
        self.fail_health = fail_health
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def execute(self, name: str, arguments: dict):
        self.calls.append(name)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # Pod Discovery 也保持异步，但 Workflow 必须等待它结束后才能调度
            # Health/Metrics/Logs 扇出。
            await asyncio.sleep(self.delay)
            if self.fail_health and name == "get_service_health":
                raise RuntimeError("health source unavailable")
            if name == "list_pods":
                return {"data": {"items": []}}
            return {"data": {"result": [{"metric": {}, "value": [1, "1"]}]}}
        finally:
            self.active -= 1

    async def specifications(self) -> list[dict]:
        return []


class FailingPlanner:
    async def decide(self, state: DiagnosisState, tool_specs: list[dict]):
        raise GatewayRequestError("planner unavailable")


def _state(max_steps: int = 12) -> DiagnosisState:
    return DiagnosisState(
        query="订单服务延迟升高",
        run_id="baseline-test",
        service="order-service",
        symptom="latency",
        max_tool_steps=max_steps,
    )


def test_service_baseline_runs_independent_queries_concurrently() -> None:
    tools = SlowBaselineTools()
    workflow = DiagnosisWorkflow(tools, str(CATALOG), max_steps=12)  # type: ignore[arg-type]
    state = _state()

    started = time.perf_counter()
    asyncio.run(workflow._baseline(state, None))
    elapsed = time.perf_counter() - started

    assert tools.calls[0] == "list_pods"
    assert tools.max_active >= 4
    assert elapsed < 0.30, "五个 80ms 独立查询不应串行累计到约 400ms"
    assert len(state.timeline) == 6
    assert len(state.evidence) == 6


def test_baseline_tool_failure_does_not_cancel_other_evidence() -> None:
    tools = SlowBaselineTools(fail_health=True, delay=0.01)
    workflow = DiagnosisWorkflow(tools, str(CATALOG), max_steps=12)  # type: ignore[arg-type]
    state = _state()
    events: list[dict] = []

    async def publish(event: dict) -> None:
        events.append(event)

    asyncio.run(workflow._baseline(state, publish))

    assert len(state.timeline) == 6
    assert len(state.evidence) == 5
    failure = next(item for item in state.timeline if item.tool_name == "get_service_health")
    assert "health source unavailable" in (failure.error or "")
    assert {item.tool_name for item in state.evidence} >= {"list_pods", "query_metrics", "query_logs"}
    assert len([event for event in events if event.get("type") == "tool"]) == 6


def test_concurrent_baseline_reserves_max_steps_before_scheduling() -> None:
    tools = SlowBaselineTools(delay=0.01)
    workflow = DiagnosisWorkflow(tools, str(CATALOG), max_steps=4)  # type: ignore[arg-type]
    state = _state(max_steps=4)

    asyncio.run(workflow._baseline(state, None))

    assert len(state.timeline) == 4
    assert len(state.evidence) == 4
    assert len(tools.calls) == 4


def test_whole_diagnosis_deadline_returns_a_final_insufficient_report() -> None:
    tools = SlowBaselineTools(delay=0.08)
    workflow = DiagnosisWorkflow(
        tools, str(CATALOG), max_steps=12, deadline_seconds=0.02,
    )  # type: ignore[arg-type]
    events: list[dict] = []

    async def publish(event: dict) -> None:
        events.append(event)

    async def execute():
        return await workflow.run(
            "订单服务延迟升高", publish,
            target="order-service", symptom="latency",
        )

    report = asyncio.run(execute())

    assert report.status == "insufficient_evidence"
    assert any(item.tool_name == "workflow_deadline" for item in report.investigation_timeline)
    assert events[-1]["type"] == "final"


def test_planner_gateway_failure_is_recorded_and_does_not_escape() -> None:
    tools = SlowBaselineTools(delay=0)
    workflow = DiagnosisWorkflow(tools, str(CATALOG))  # type: ignore[arg-type]
    state = _state()

    asyncio.run(workflow._investigate(
        state, None, planner=FailingPlanner(),  # type: ignore[arg-type]
    ))

    assert len(state.timeline) == 1
    assert state.timeline[0].tool_name == "llm_planner"
    assert "planner unavailable" in (state.timeline[0].error or "")
