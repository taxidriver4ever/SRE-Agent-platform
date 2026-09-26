"""证据驱动的通用与专项 SRE 工作流实现。"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.conversation import ConversationService
from app.agent.observability import agent_call_context
from app.core.errors import TaskOwnershipLostError
from app.conversation_memory import ConversationCompactionService
from app.code_state import CodeStateService
from app.evidence import build_source_references, normalize_tool_result
from app.workflow.evidence_gate import (
    build_report, is_direct_evidence, source_for_tool, supports_conclusion,
)
from app.llm.base import LLM
from app.llm.gateway import GatewayError
from app.mcp_clients import FastMCPToolClient
from app.repositories import RepositoryRegistry
from app.workflow.catalog import ServiceCatalog
from app.workflow.models import (
    CandidateCause,
    DiagnosisReport,
    DiagnosisSynthesis,
    DiagnosisState,
    Evidence,
    ToolCallRecord,
    WorkflowPhase,
)
from app.workflow.planner import EvidencePlanner
from app.workflow.runtime_extractor import (
    extract_git_sha, extract_pod_runtime, extract_trace_id, find_pod_name,
)
from app.workflow.runtime import NoopWorkflowRuntime, ToolExecutionClaim, WorkflowRuntime

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
logger = logging.getLogger(__name__)


class DiagnosisWorkflow:
    """执行固定阶段、有限步数且至少双证据验证的单 Agent 工作流。"""

    def __init__(
        self,
        tools: FastMCPToolClient,
        catalog_path: str,
        max_steps: int = 12,
        llm: LLM | None = None,
        repository_registry: RepositoryRegistry | None = None,
        context_service: ConversationCompactionService | None = None,
        conversation_service: ConversationService | None = None,
        code_state_service: CodeStateService | None = None,
        kubernetes_namespace: str = "sre-lab",
        deadline_seconds: float = 240,
        history_retrieval: Any = None,
        default_project_id: str = "sre-lab",
    ) -> None:
        self.tools = tools
        self.catalog = ServiceCatalog(catalog_path)
        self.max_steps = max_steps
        # Workflow 只依赖统一 LLM 协议；具体 Provider 必须由 GatewayLLM 隔离。
        self.llm = llm
        self.repository_registry = repository_registry
        self.context_service = context_service
        self.history_retrieval = history_retrieval
        self.default_project_id = default_project_id
        self.conversation_service = conversation_service
        self.code_state_service = code_state_service
        self.kubernetes_namespace = kubernetes_namespace
        self.deadline_seconds = max(0.01, min(float(deadline_seconds), 3600.0))
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def run(
        self,
        query: str,
        on_event: EventCallback | None = None,
        *,
        conversation_id: str | None = None,
        user_id: str | None = None,
        target: str | None = None,
        symptom: str | None = None,
        system_scan: bool = False,
        selected_services: list[str] | None = None,
        resume_state: DiagnosisState | None = None,
        runtime: WorkflowRuntime | None = None,
    ) -> DiagnosisReport:
        """从 TRIAGE 走到 REPORT；模型不能跳过基线观测或证据验证。"""
        runtime = runtime or NoopWorkflowRuntime()
        if resume_state is None:
            run_id = uuid4().hex
            # 没有持久会话的脚本调用使用 run_id 作为独立内存会话，不共享上下文。
            state = DiagnosisState(
                query=query,
                run_id=run_id,
                conversation_id=conversation_id or run_id,
                user_id=user_id,
                service=target or "unknown",
                symptom=symptom or "待确定",
                max_tool_steps=self.max_steps,
                selected_services=list(dict.fromkeys(selected_services or [])),
            )
        else:
            # Crash Recovery 保留 logical run_id、Evidence、Timeline 与 Runtime Identity。
            state = resume_state
            state.max_tool_steps = self.max_steps
        await runtime.initialize(state)
        planner = EvidencePlanner(self.llm) if self.llm is not None else None
        if resume_state is None and self.conversation_service and state.user_id:
            self.conversation_service.append(
                state.user_id,
                state.conversation_id,
                "user",
                {"message": query},
                message_type="user",
                run_id=state.run_id,
                message_id=self._conversation_operation_id(state.run_id, "user"),
            )
        try:
            report = await asyncio.wait_for(
                self._execute_diagnosis(
                    state, planner, on_event, system_scan=system_scan, runtime=runtime,
                ),
                timeout=self.deadline_seconds,
            )
        except TimeoutError:
            record = ToolCallRecord(
                tool_name="workflow_deadline",
                arguments={"deadline_seconds": self.deadline_seconds},
                result_summary="整轮诊断达到截止时间，保留已收集证据并安全结束",
                timestamp=datetime.now(timezone.utc),
                duration_ms=int(self.deadline_seconds * 1000),
                error=f"diagnosis exceeded {self.deadline_seconds:g} seconds",
            )
            state.timeline.append(record)
            if on_event:
                await on_event({"type": "tool", "record": record.model_dump(mode="json")})
            state.synthesis = DiagnosisSynthesis(
                status="insufficient_evidence",
                root_cause="诊断达到截止时间，现有证据不足以确认根因",
                confidence=0.0,
            )
            if WorkflowPhase.VERIFY not in state.phases:
                await self._phase(state, WorkflowPhase.VERIFY, on_event, runtime)
            report = self._report(state)
            await runtime.phase_completed(state, WorkflowPhase.VERIFY)
            await self._phase(state, WorkflowPhase.REPORT, on_event, runtime)
            await runtime.phase_completed(state, WorkflowPhase.REPORT)
            await self._phase(state, WorkflowPhase.END, on_event, runtime)
            await runtime.phase_completed(state, WorkflowPhase.END)
            report.workflow_phases = state.phases
        await self._update_conversation_context(state, report)
        if on_event:
            await on_event({"type": "final", "report": report.model_dump(mode="json")})
        return report

    async def _execute_diagnosis(
        self,
        state: DiagnosisState,
        planner: EvidencePlanner | None,
        on_event: EventCallback | None,
        *,
        system_scan: bool,
        runtime: WorkflowRuntime,
    ) -> DiagnosisReport:
        """执行受整轮 deadline 约束的诊断阶段；报告持久化在截止时间之外完成。"""
        if not runtime.should_skip_phase(WorkflowPhase.START):
            await self._phase(state, WorkflowPhase.START, on_event, runtime)
            await runtime.phase_completed(state, WorkflowPhase.START)
        if system_scan and not runtime.should_skip_phase(WorkflowPhase.SYSTEM_SCAN):
            await self._system_scan(state, on_event, runtime=runtime)
            await runtime.phase_completed(state, WorkflowPhase.SYSTEM_SCAN)
        if not runtime.should_skip_phase(WorkflowPhase.TRIAGE):
            await self._triage(state, on_event, system_scan=system_scan, runtime=runtime)
            await runtime.phase_completed(state, WorkflowPhase.TRIAGE)
        if not runtime.should_skip_phase(WorkflowPhase.BASELINE_OBSERVATION):
            await self._history_context(state)
            await self._baseline(state, on_event, system_scan=system_scan, runtime=runtime)
            await runtime.phase_completed(state, WorkflowPhase.BASELINE_OBSERVATION)
        if not runtime.should_skip_phase(WorkflowPhase.ANALYZE):
            await self._analyze(state, on_event, runtime=runtime)
            await runtime.phase_completed(state, WorkflowPhase.ANALYZE)
        if not runtime.should_skip_phase(WorkflowPhase.INVESTIGATE):
            await self._investigate(
                state, on_event, planner=planner, system_scan=system_scan, runtime=runtime,
            )
            await runtime.phase_completed(state, WorkflowPhase.INVESTIGATE)
        if not runtime.should_skip_phase(WorkflowPhase.VERIFY):
            await self._phase(state, WorkflowPhase.VERIFY, on_event, runtime)
            # Planner 直接消费有界 Evidence，不需要等待会话压缩。压缩在报告落库后
            # 后台执行，避免把最多 30 秒的增强任务计入同步诊断延迟。
            with agent_call_context(
                correlation_id=state.run_id, phase=WorkflowPhase.VERIFY.value,
                diagnosis_id=getattr(runtime, "diagnosis_id", None),
            ):
                await self._synthesize_with_gateway(state, planner, on_event)
            await runtime.phase_completed(state, WorkflowPhase.VERIFY)
        report = self._report(state)
        if not runtime.should_skip_phase(WorkflowPhase.REPORT):
            await self._phase(state, WorkflowPhase.REPORT, on_event, runtime)
            await runtime.phase_completed(state, WorkflowPhase.REPORT)
        if not runtime.should_skip_phase(WorkflowPhase.END):
            await self._phase(state, WorkflowPhase.END, on_event, runtime)
            await runtime.phase_completed(state, WorkflowPhase.END)
        report.workflow_phases = state.phases
        return report

    async def _history_context(self, state: DiagnosisState) -> None:
        """Historical cases are advisory context, never current-runtime evidence."""
        if state.history_retrieved:
            return
        state.history_retrieved = True
        if not state.user_id:
            return
        if self.context_service:
            try:
                snapshot = await asyncio.to_thread(
                    self.context_service.repository.active_snapshot, state.user_id, state.conversation_id)
                state.short_context = json.dumps({"summary": snapshot.summary[:1200],
                    "context_state": snapshot.state.model_dump(mode="json")}, ensure_ascii=False)
            except Exception as exc:
                logger.warning("Short context unavailable: reason=%s", type(exc).__name__)
        if self.history_retrieval is not None:
            from app.security.scope import current_task_scope
            scope = current_task_scope()
            if scope is not None and scope.user_id != state.user_id:
                return
            try:
                state.long_term_history = await self.history_retrieval.for_incident(
                    state.user_id, scope.project_id if scope else self.default_project_id,
                    state.service, state.query, scope.task_id if scope else None)
            except Exception as exc:
                logger.warning("History Retrieval skipped: reason=%s", type(exc).__name__)
                state.long_term_history = "[]"

    async def _phase(
        self, state: DiagnosisState, phase: WorkflowPhase, callback: EventCallback | None,
        runtime: WorkflowRuntime | None = None,
    ) -> None:
        """记录状态迁移并向 SSE 客户端发送进度事件。"""
        if phase not in state.phases:
            state.phases.append(phase)
        if runtime is not None:
            await runtime.phase_started(state, phase)
        if callback:
            await callback({"type": "phase", "phase": phase.value})

    async def _system_scan(
        self, state: DiagnosisState, callback: EventCallback | None,
        *, runtime: WorkflowRuntime | None = None,
    ) -> None:
        """整体巡检先建立全局服务、Pod 和错误率视图，不预设故障服务。"""
        await self._phase(state, WorkflowPhase.SYSTEM_SCAN, callback, runtime)
        # Deployment 与 Pod 清单都是独立的只读发现操作。它们需要先于全局
        # Baseline 完成，但彼此没有数据依赖，可以安全并发。
        await self._call_concurrently(state, callback, [
            ("list_deployments", {}, "系统 Deployment 清单"),
            ("list_pods", {}, "系统 Pod、版本与运行状态"),
        ], runtime=runtime, phase=WorkflowPhase.SYSTEM_SCAN)

    async def _triage(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        system_scan: bool = False,
        runtime: WorkflowRuntime | None = None,
    ) -> None:
        """确定 service、symptom、environment 与默认最近 30 分钟窗口。"""
        await self._phase(state, WorkflowPhase.TRIAGE, callback, runtime)
        analysis_input = state.query
        if state.service not in self.catalog.services:
            state.service = self.catalog.resolve(f"{state.service} {analysis_input}")
        metadata = self.catalog.services.get(state.service, {})
        state.language = str(metadata.get("language", "unknown"))
        state.repository = state.service if state.service in self.catalog.services else None
        state.source_code_location = metadata.get("source_path")
        state.repository_url = metadata.get("repository_url")
        # 展开服务目录中的传递依赖，使“上游 -> 中间服务 -> 实际故障下游”的
        # 调查可以继续推进，但所有服务名仍来自目录而非模型猜测。
        direct_dependencies = [str(item) for item in metadata.get("dependencies", [])]
        # 用户多选服务是起始调查集合，不是硬边界；纳入显式 Context 后仍允许
        # Trace、日志和目录依赖继续发现其他资源。
        direct_dependencies = list(dict.fromkeys([
            *direct_dependencies,
            *(service for service in state.selected_services if service != state.service),
        ]))
        query_lower = analysis_input.lower()
        retry_query = any(term in query_lower for term in ("重试", "retry", "放大", "storm"))
        mentioned_dependencies = [
            dependency for dependency in direct_dependencies
            if dependency.lower() in query_lower
            or any(
                str(alias).lower() in query_lower
                for alias in self.catalog.services.get(dependency, {}).get("aliases", [])
            )
        ]
        # 重试调查优先沿用户明确提到的那条调用链推进；否则四个并列下游会在
        # 到达真正末端依赖前耗尽工具预算。
        dependency_queue = mentioned_dependencies if retry_query and mentioned_dependencies else direct_dependencies
        expanded_dependencies: list[str] = []
        while dependency_queue:
            dependency = dependency_queue.pop(0)
            if dependency in expanded_dependencies or dependency == state.service:
                continue
            expanded_dependencies.append(dependency)
            dependency_metadata = self.catalog.services.get(dependency, {})
            dependency_queue.extend(str(item) for item in dependency_metadata.get("dependencies", []))
        state.dependencies = expanded_dependencies
        text = analysis_input.lower()
        routed_symptom = state.symptom.lower()
        if state.symptom != "待确定" and any(word in routed_symptom for word in ("重试", "retry", "依赖", "dependency", "timeout")):
            state.symptom = "dependency_timeout"
        elif state.symptom != "待确定" and any(word in routed_symptom for word in ("重启", "restart", "oom", "memory")):
            state.symptom = "pod_restart"
        elif state.symptom != "待确定" and any(word in routed_symptom for word in ("latency", "slow", "delay")):
            state.symptom = "latency"
        elif state.symptom != "待确定" and any(word in routed_symptom for word in ("5xx", "error", "failure")):
            state.symptom = "5xx"
        elif state.symptom != "待确定":
            state.symptom = "general_incident"
        elif any(word in text for word in ("重试", "retry", "依赖", "dependency", "timeout", "超时")):
            state.symptom = "dependency_timeout"
        elif any(word in text for word in ("慢", "延迟", "latency")):
            state.symptom = "latency"
        elif any(word in text for word in ("500", "错误", "error", "失败")):
            state.symptom = "5xx"
        elif any(word in text for word in ("重启", "oom", "restart", "内存")):
            state.symptom = "pod_restart"
        else:
            state.symptom = "general_incident"
        # 即使用户没有明确服务，也先列出只读 Service/Deployment，避免凭空选定根因。
        if state.service == "unknown" and not system_scan:
            await self._call(
                state, "list_deployments", {}, "K8s 服务发现", callback,
                runtime=runtime, phase=WorkflowPhase.TRIAGE,
            )

    async def _baseline(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        system_scan: bool = False,
        runtime: WorkflowRuntime | None = None,
    ) -> None:
        """硬性采集健康、延迟、错误率、CPU/内存与异常日志。"""
        await self._phase(state, WorkflowPhase.BASELINE_OBSERVATION, callback, runtime)
        if system_scan and state.service == "unknown":
            await self._call_concurrently(state, callback, [
                (
                    "query_metrics",
                    {"query": "sum by (service) (up)", "time_range_minutes": state.time_range_minutes},
                    "全服务健康基线",
                ),
                (
                    "query_metrics",
                    {
                        "query": 'sum by (service) (rate(http_server_requests_seconds_count{status=~"5.."}[5m]))',
                        "time_range_minutes": state.time_range_minutes,
                    },
                    "全服务 HTTP 5xx 速率",
                ),
                (
                    "query_metrics",
                    {
                        "query": (
                            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{self.kubernetes_namespace}"}}[5m])) '
                            f'or sum by (pod) (container_memory_working_set_bytes{{namespace="{self.kubernetes_namespace}"}})'
                        ),
                        "time_range_minutes": state.time_range_minutes,
                    },
                    "全局 Pod CPU/内存",
                ),
                (
                    "query_logs",
                    {"time_range_minutes": state.time_range_minutes, "level": "error", "limit": 50},
                    "全局异常日志",
                ),
            ], runtime=runtime, phase=WorkflowPhase.BASELINE_OBSERVATION)
            return
        if state.service == "unknown":
            await self._call_concurrently(state, callback, [
                (
                    "query_metrics",
                    {"query": "sum by (service) (up)", "time_range_minutes": state.time_range_minutes},
                    "全服务健康基线",
                ),
                (
                    "query_logs",
                    {"time_range_minutes": state.time_range_minutes, "limit": 50},
                    "跨服务近期日志",
                ),
            ], runtime=runtime, phase=WorkflowPhase.BASELINE_OBSERVATION)
            return
        service = state.service
        common = {"service": service, "time_range_minutes": state.time_range_minutes}
        # Pod 清单必须先于聚合指标采集，以便识别单实例异常与混合镜像版本。
        pods = await self._call(
            state, "list_pods", {"label_selector": f"app={service}"}, "服务 Pod 与运行版本", callback,
            runtime=runtime, phase=WorkflowPhase.BASELINE_OBSERVATION,
        )
        self._extract_pod_runtime(state, pods)
        state.pod_name = state.pod_name or self._find_pod_name(pods, service)
        if runtime is not None:
            await runtime.checkpoint(
                state, WorkflowPhase.BASELINE_OBSERVATION,
                f"pod-runtime:{len(state.timeline)}",
            )
        # 优先查询 P95 直方图；服务未开启 histogram 时 INVESTIGATE 仍可依赖日志、Trace 和数据库证据。
        latency = (
            f'histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket'
            f'{{service="{service}"}}[5m])) by (le))'
        )
        errors = f'sum(rate(http_server_requests_seconds_count{{service="{service}",status=~"5.."}}[5m]))'
        resources = (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{self.kubernetes_namespace}",pod=~"{service}.*"}}[5m])) '
            f'or sum by (pod) (container_memory_working_set_bytes{{namespace="{self.kubernetes_namespace}",pod=~"{service}.*"}})'
        )
        # Pod Discovery 与运行版本提取必须先完成；以下五个查询只依赖已经确定
        # 的 service/time range/runtime context，彼此无依赖，按真实完成时间入库。
        await self._call_concurrently(state, callback, [
            ("get_service_health", {"service": service}, "服务健康"),
            ("query_metrics", {"query": latency, "time_range_minutes": state.time_range_minutes}, "HTTP P95"),
            ("query_metrics", {"query": errors, "time_range_minutes": state.time_range_minutes}, "HTTP 5xx 速率"),
            ("query_metrics", {"query": resources, "time_range_minutes": state.time_range_minutes}, "Pod 级 CPU/内存"),
            ("query_logs", {**common, "limit": 20}, "近期服务日志"),
        ], runtime=runtime, phase=WorkflowPhase.BASELINE_OBSERVATION)

    async def _analyze(
        self, state: DiagnosisState, callback: EventCallback | None,
        *, runtime: WorkflowRuntime | None = None,
    ) -> None:
        """候选原因由后续 Planner 从 Evidence 生成，Workflow 不预置故障答案。"""
        await self._phase(state, WorkflowPhase.ANALYZE, callback, runtime)
        state.candidates = []

    async def _investigate(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        planner: EvidencePlanner | None,
        system_scan: bool = False,
        runtime: WorkflowRuntime | None = None,
    ) -> None:
        """由 Planner 根据当前 Evidence 逐步选择工具，不按 Service/Case 分支。"""
        await self._phase(state, WorkflowPhase.INVESTIGATE, callback, runtime)
        del system_scan
        if planner is None or state.service == "unknown":
            return
        # INVESTIGATE 中断在 Tool commit 之前时，优先恢复数据库中的 RUNNING
        # logical step。真实 Tool 是只读的，允许 crash window 内 at-least-once；
        # Runtime 会强制复用原 idempotency_key、Evidence ID 和 Step 行。
        pending_calls = runtime.pending_tool_calls(WorkflowPhase.INVESTIGATE) if runtime else []
        for pending in pending_calls:
            if len(state.timeline) >= self.max_steps:
                break
            await self._call(
                state, pending.tool_name, pending.arguments, pending.title, callback,
                parent_evidence_ids=pending.parent_evidence_ids,
                runtime=runtime, phase=WorkflowPhase.INVESTIGATE,
            )
        investigation_tools = {
            "query_metrics", "query_logs", "query_trace", "query_slow_queries",
            "query_sql_digest", "explain_sql", "get_pod", "get_pod_events",
            "get_restart_count", "get_deployment", "get_container_image",
            "get_repository", "get_current_commit", "get_commit", "get_previous_commit",
            "list_changed_files", "get_commit_diff", "search_code_state",
            "read_file_at_commit", "search_code",
        }
        tool_specs = [
            item for item in await self.tools.specifications()
            if item.get("name") in investigation_tools
        ]
        while len(state.timeline) < self.max_steps:
            planner_started_at = datetime.now(timezone.utc)
            planner_started = time.perf_counter()
            try:
                with agent_call_context(
                    correlation_id=state.run_id, phase=WorkflowPhase.INVESTIGATE.value,
                    diagnosis_id=getattr(runtime, "diagnosis_id", None),
                ):
                    decision = await planner.decide(state, tool_specs)
            except GatewayError as exc:
                record = ToolCallRecord(
                    tool_name="llm_planner",
                    arguments={"evidence_count": len(state.evidence)},
                    result_summary="Planner 请求失败，停止扩展并交由 Evidence Gate 判定",
                    timestamp=planner_started_at,
                    duration_ms=int((time.perf_counter() - planner_started) * 1000),
                    error=self._exception_text(exc),
                )
                state.timeline.append(record)
                if callback:
                    await callback({"type": "tool", "record": record.model_dump(mode="json")})
                break
            if decision.action != "tool":
                break
            signature = json.dumps(
                [decision.tool_name, decision.arguments], ensure_ascii=False, sort_keys=True
            )
            repeated = any(
                json.dumps([item.tool_name, item.arguments], ensure_ascii=False, sort_keys=True) == signature
                for item in state.timeline
            )
            if repeated:
                break
            if decision.reason:
                state.candidates = [
                    CandidateCause(cause=decision.title, reason=decision.reason, priority=5)
                ]
            result = await self._call(
                state,
                decision.tool_name or "",
                decision.arguments,
                decision.title,
                callback,
                parent_evidence_ids=decision.parent_evidence_ids,
                runtime=runtime,
                phase=WorkflowPhase.INVESTIGATE,
                reason=decision.reason,
            )
            if decision.tool_name in {"list_pods", "get_pod"}:
                self._extract_pod_runtime(state, result)
            elif decision.tool_name == "get_container_image":
                state.runtime_commit = self._extract_git_sha(result) or state.runtime_commit
            if runtime is not None:
                await runtime.checkpoint(
                    state, WorkflowPhase.INVESTIGATE,
                    f"planner-round:{len(state.timeline)}",
                )

    async def _call(
        self,
        state: DiagnosisState,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        callback: EventCallback | None,
        parent_evidence_ids: list[str] | None = None,
        runtime: WorkflowRuntime | None = None,
        phase: WorkflowPhase = WorkflowPhase.INVESTIGATE,
        reason: str | None = None,
    ) -> Any:
        """执行工具并记录 timestamp/duration/error；到达 max_steps 后拒绝继续。"""
        if len(state.timeline) >= self.max_steps:
            return None
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        active_runtime = runtime or NoopWorkflowRuntime()
        claim = await active_runtime.begin_tool(
            state, phase, tool_name, arguments, title, parent_evidence_ids or [],
        )
        if claim.completed:
            if claim.evidence is not None and all(
                item.evidence_id != claim.evidence.evidence_id for item in state.evidence
            ):
                state.evidence.append(claim.evidence)
            if claim.record is not None and not any(
                item.tool_name == claim.record.tool_name and item.arguments == claim.record.arguments
                for item in state.timeline
            ):
                state.timeline.append(claim.record)
            return claim.result
        error: str | None = None
        evidence_id: str | None = claim.evidence_id
        evidence: Evidence | None = None
        result: Any = None
        if self.conversation_service and state.user_id:
            self.conversation_service.append(
                state.user_id,
                state.conversation_id,
                "assistant",
                {"tool_name": tool_name, "arguments": arguments},
                message_type="tool_call",
                run_id=state.run_id,
                tool_name=tool_name,
                message_id=self._conversation_operation_id(claim.idempotency_key, "call"),
            )
        try:
            with agent_call_context(
                correlation_id=state.run_id, diagnosis_id=getattr(active_runtime, "diagnosis_id", None),
                phase=phase.value, step_id=claim.step_id, logical_step_key=claim.idempotency_key,
                tool_name=tool_name, reason=reason or title,
                evidence_id=claim.evidence_id,
            ):
                result = await self.tools.execute(tool_name, arguments)
            references = build_source_references(
                tool_name,
                arguments,
                result,
                namespace=self.kubernetes_namespace,
                repository_url=state.repository_url,
            )
            normalized = normalize_tool_result(tool_name, arguments, result, references)
            if self.conversation_service and state.user_id:
                evidence_id = self.conversation_service.append(
                    state.user_id,
                    state.conversation_id,
                    "assistant",
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result,
                        "normalized_result": normalized.model_dump(mode="json"),
                        "source_references": [item.model_dump(mode="json") for item in references],
                        "parent_evidence_ids": parent_evidence_ids or [],
                    },
                    message_type="tool_result",
                    run_id=state.run_id,
                    tool_name=tool_name,
                    message_id=evidence_id,
                )
            else:
                evidence_id = f"ev_{uuid4().hex[:16]}"
            summary = normalized.summary
            evidence = Evidence(
                source=self._source(tool_name), source_type=self._source(tool_name),
                evidence_type={"Prometheus": "metric", "Elasticsearch": "log", "SkyWalking": "trace"}.get(self._source(tool_name), "runtime"),
                service_name=arguments.get("service_name") or arguments.get("service") or state.service,
                trace_id=next(iter(normalized.structured_data.get("trace_ids", [])), None),
                severity=next((row.get("level") for row in normalized.structured_data.get("logs", []) if row.get("level")), None),
                raw_reference=references[0].uri if references else None,
                tool_name=tool_name, title=title, detail=summary, summary=summary,
                timestamp=started_at, evidence_id=evidence_id,
                structured_data=normalized.structured_data,
                source_references=references, reference=references,
                parent_evidence_ids=parent_evidence_ids or [],
                next_hints=normalized.next_hints,
                supports_conclusion=normalized.status == "success" and not normalized.structured_data.get("empty", False) and self._supports_conclusion(tool_name, summary),
                direct_evidence=normalized.status == "success" and not normalized.structured_data.get("empty", False) and self._is_direct_evidence(tool_name, arguments, summary),
            )
            state.evidence.append(evidence)
        except TaskOwnershipLostError:
            raise
        except Exception as exc:
            error = self._exception_text(exc)
            summary = ""
            if self.conversation_service and state.user_id:
                self.conversation_service.append(
                    state.user_id,
                    state.conversation_id,
                    "assistant",
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "error": error,
                        "normalized_result": {
                            "tool": tool_name,
                            "status": "error",
                            "summary": error,
                            "data": {},
                            "structured_data": {},
                            "references": [],
                            "next_hints": [],
                        },
                        "parent_evidence_ids": parent_evidence_ids or [],
                    },
                    message_type="tool_result",
                    run_id=state.run_id,
                    tool_name=tool_name,
                    message_id=evidence_id,
                )
        record = ToolCallRecord(
            tool_name=tool_name, arguments=arguments, result_summary=summary,
            timestamp=started_at, duration_ms=int((time.perf_counter() - started) * 1000), error=error,
            evidence_id=evidence_id,
        )
        state.timeline.append(record)
        await active_runtime.complete_tool(
            state, phase, claim, record, evidence, result,
        )
        if callback:
            await callback({"type": "tool", "record": record.model_dump(mode="json")})
        return result

    async def _call_concurrently(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        calls: list[tuple[str, dict[str, Any], str]],
        *,
        runtime: WorkflowRuntime | None = None,
        phase: WorkflowPhase = WorkflowPhase.BASELINE_OBSERVATION,
    ) -> list[Any]:
        """并发执行一组互不依赖的只读调用，并在调度前统一预留预算。

        ``_call`` 会把工具异常转换为失败 ToolCall，因此单个数据源失败不会
        取消其他任务。callback 或持久化层的普通异常仍隔离；所有权丢失和取消
        则立即终止同组请求。时间线与 Evidence 的 append 都发生在事件循环单线程内，
        每条记录在发送 SSE 前已经完整写入。
        """
        available = max(0, self.max_steps - len(state.timeline))
        selected = calls[:available]
        if not selected:
            return []
        async def invoke(tool_name, arguments, title):
            try:
                return await self._call(
                    state, tool_name, arguments, title, callback,
                    runtime=runtime, phase=phase,
                )
            except (TaskOwnershipLostError, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.warning("concurrent baseline task failed outside tool boundary: %s", self._exception_text(exc))
                return None

        tasks = [asyncio.create_task(invoke(*call)) for call in selected]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            # Fencing/cancellation are fatal, unlike an isolated data-source error.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _conversation_operation_id(idempotency_key: str | None, suffix: str) -> str | None:
        if not idempotency_key:
            return None
        import hashlib
        return hashlib.sha256(f"{idempotency_key}:{suffix}".encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _exception_text(exc: BaseException) -> str:
        detail = str(exc).strip()
        return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__

    @staticmethod
    def _compact_result(value: Any, limit: int = 900) -> str:
        """报告只展示有界预览；完整 Tool Result 已进入 Conversation Store。"""
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        return text if len(text) <= limit else f"{text[:limit]}…（完整结果见 Conversation Store）"

    # 兼容现有测试与扩展；实现已移动到职责单一的辅助模块。
    _source = staticmethod(source_for_tool)
    _find_pod_name = staticmethod(find_pod_name)
    _extract_git_sha = staticmethod(extract_git_sha)
    _extract_trace_id = staticmethod(extract_trace_id)
    _supports_conclusion = staticmethod(supports_conclusion)
    _is_direct_evidence = staticmethod(is_direct_evidence)

    def _extract_pod_runtime(self, state: DiagnosisState, payload: Any) -> None:
        extract_pod_runtime(state, payload, self.repository_registry)

    async def _update_conversation_context(
        self,
        state: DiagnosisState,
        report: DiagnosisReport,
    ) -> None:
        """保存助手消息，并在达到阈值时合并跨轮 Context State。

        上下文压缩是增强链路：网关暂时不可用或模型返回非法 JSON 时，诊断报告
        仍然应该正常返回。未成功压缩的 Message 会继续留在增量区，下一轮
        可以再次触发，而不是错误推进压缩游标。
        """
        if self.context_service is None or self.conversation_service is None or not state.user_id:
            return
        try:
            self.conversation_service.append(
                state.user_id,
                state.conversation_id,
                "assistant",
                {"report": report.model_dump(mode="json")},
                message_type="assistant",
                run_id=state.run_id,
                message_id=self._conversation_operation_id(state.run_id, "final-report"),
            )
        except Exception as exc:
            # 诊断工具证据已经在各步骤尽力落库；最终持久化短暂失败不能把一个
            # 已完成的只读诊断变成 HTTP 500。
            logger.warning(
                "conversation report persistence failed: conversation_id=%s error=%s",
                state.conversation_id,
                self._exception_text(exc),
            )
            return
        try:
            report.context_compaction.update({
                "storage": "mysql",
                "compaction_ratio": str(self.context_service.compaction_ratio),
                "active_context_tokens": self.context_service.active_token_count(
                    state.user_id, state.conversation_id
                ),
                "compression_count": self.context_service.repository.compaction_count(
                    state.user_id, state.conversation_id
                ),
            })
        except Exception as exc:
            logger.warning(
                "conversation context statistics failed: conversation_id=%s error=%s",
                state.conversation_id,
                self._exception_text(exc),
            )
            return
        # 报告持久化后立即允许 SSE 返回 final。压缩是增强任务，即使本地模型
        # 冷启动或生成缓慢，也不能让已经完成的诊断在页面上继续卡数分钟。
        task = asyncio.create_task(
            self._compact_after_report(state.user_id, state.conversation_id),
            name=f"compact-{state.conversation_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _compact_after_report(self, user_id: str, conversation_id: str) -> None:
        if not self._has_multiple_user_turns(user_id, conversation_id):
            return
        try:
            await self.context_service.maybe_compact(user_id, conversation_id)
        except (GatewayError, ValueError, TimeoutError) as exc:
            logger.warning(
                "background conversation compaction failed: conversation_id=%s error=%s",
                conversation_id,
                self._exception_text(exc),
            )
        except Exception:
            logger.exception(
                "unexpected background compaction failure: conversation_id=%s",
                conversation_id,
            )

    async def _compact_context_before_summary(self, state: DiagnosisState) -> None:
        """在本轮 LLM 摘要前压缩达到预算阈值的持久化 Conversation Message。

        压缩失败不推进 MySQL 边界，也不阻断诊断。后续摘要继续使用未压缩原文，
        报告阶段还会再次尝试压缩，因此不会静默丢失调查信息。
        """
        if (
            self.context_service is None
            or not state.user_id
            or not self._has_multiple_user_turns(state.user_id, state.conversation_id)
        ):
            return
        try:
            await asyncio.wait_for(
                self.context_service.maybe_compact(state.user_id, state.conversation_id),
                timeout=30,
            )
        except (GatewayError, ValueError, TimeoutError):
            return

    def _has_multiple_user_turns(self, user_id: str, conversation_id: str) -> bool:
        """单轮诊断保留原始证据；从第二个用户回合起才需要压缩上下文。"""
        if self.context_service is None:
            return False
        try:
            snapshot = self.context_service.repository.active_snapshot(user_id, conversation_id)
        except Exception:
            return False
        return sum(message.get("role") == "user" for message in snapshot.pending_messages) >= 2

    async def _synthesize_with_gateway(
        self,
        state: DiagnosisState,
        planner: EvidencePlanner | None,
        callback: EventCallback | None,
    ) -> None:
        """让模型只综合 Evidence；失败时保留原始证据并安全降级。"""
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        error: str | None = None
        if planner is None:
            state.synthesis = DiagnosisSynthesis(
                status="insufficient_evidence",
                root_cause="证据综合模型不可用，无法确认根因",
                confidence=0.0,
            )
        else:
            try:
                state.synthesis = await planner.synthesize(state)
            except GatewayError as exc:
                error = self._exception_text(exc)
                state.synthesis = DiagnosisSynthesis(
                    status="insufficient_evidence",
                    root_cause="证据综合失败，无法确认根因",
                    confidence=0.0,
                )
            state.prompt_tokens = planner.prompt_tokens
            state.completion_tokens = planner.completion_tokens
            state.structured_output_retry_count = planner.structured_output_retry_count
        summary = state.synthesis.root_cause if state.synthesis else ""
        record = ToolCallRecord(
            tool_name="llm_evidence_synthesis",
            arguments={"evidence_count": len(state.evidence)},
            result_summary=summary,
            timestamp=started_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=error,
        )
        state.timeline.append(record)
        if callback:
            await callback({"type": "tool", "record": record.model_dump(mode="json")})
    def _report(self, state: DiagnosisState) -> DiagnosisReport:
        """兼容旧扩展点；Evidence Gate 与报告构建位于独立模块。"""
        return build_report(state)
