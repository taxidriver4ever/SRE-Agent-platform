"""证据驱动的通用与专项 SRE 工作流实现。"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.conversation import ConversationService
from app.conversation_memory import ConversationCompactionService
from app.code_state import CodeStateService
from app.evidence import build_source_references, normalize_tool_result
from app.llm.base import LLM
from app.llm.gateway import GatewayError
from app.mcp_clients import FastMCPToolClient
from app.repositories import RepositoryRegistry
from app.workflow.catalog import ServiceCatalog
from app.workflow.models import (
    CandidateCause,
    DiagnosisReport,
    DiagnosisFinding,
    DiagnosisSynthesis,
    DiagnosisState,
    Evidence,
    ToolCallRecord,
    WorkflowPhase,
)
from app.workflow.planner import EvidencePlanner

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
    ) -> None:
        self.tools = tools
        self.catalog = ServiceCatalog(catalog_path)
        self.max_steps = max_steps
        # Workflow 只依赖统一 LLM 协议；具体 Provider 必须由 GatewayLLM 隔离。
        self.llm = llm
        self.repository_registry = repository_registry
        self.context_service = context_service
        self.conversation_service = conversation_service
        self.code_state_service = code_state_service
        self.kubernetes_namespace = kubernetes_namespace
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
    ) -> DiagnosisReport:
        """从 TRIAGE 走到 REPORT；模型不能跳过基线观测或证据验证。"""
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
        )
        planner = EvidencePlanner(self.llm) if self.llm is not None else None
        if self.conversation_service and state.user_id:
            self.conversation_service.append(
                state.user_id,
                state.conversation_id,
                "user",
                {"message": query},
                message_type="user",
                run_id=state.run_id,
            )
        await self._phase(state, WorkflowPhase.START, on_event)
        if system_scan:
            await self._system_scan(state, on_event)
        await self._triage(state, on_event, system_scan=system_scan)
        await self._baseline(state, on_event, system_scan=system_scan)
        await self._analyze(state, on_event)
        await self._investigate(state, on_event, planner=planner, system_scan=system_scan)
        await self._phase(state, WorkflowPhase.VERIFY, on_event)
        # 在生成模型摘要之前先检查阈值，避免把已经足够大的跨轮增量继续原样传递。
        await self._compact_context_before_summary(state)
        await self._synthesize_with_gateway(state, planner, on_event)
        report = self._report(state)
        await self._phase(state, WorkflowPhase.REPORT, on_event)
        await self._phase(state, WorkflowPhase.END, on_event)
        report.workflow_phases = state.phases
        await self._update_conversation_context(state, report)
        if on_event:
            await on_event({"type": "final", "report": report.model_dump(mode="json")})
        return report

    async def _phase(self, state: DiagnosisState, phase: WorkflowPhase, callback: EventCallback | None) -> None:
        """记录状态迁移并向 SSE 客户端发送进度事件。"""
        state.phases.append(phase)
        if callback:
            await callback({"type": "phase", "phase": phase.value})

    async def _system_scan(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """整体巡检先建立全局服务、Pod 和错误率视图，不预设故障服务。"""
        await self._phase(state, WorkflowPhase.SYSTEM_SCAN, callback)
        await self._call(state, "list_deployments", {}, "系统 Deployment 清单", callback)
        await self._call(state, "list_pods", {}, "系统 Pod、版本与运行状态", callback)

    async def _triage(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        system_scan: bool = False,
    ) -> None:
        """确定 service、symptom、environment 与默认最近 30 分钟窗口。"""
        await self._phase(state, WorkflowPhase.TRIAGE, callback)
        analysis_input = state.query
        if state.service not in self.catalog.services:
            state.service = self.catalog.resolve(f"{state.service} {analysis_input}")
        metadata = self.catalog.services.get(state.service, {})
        state.language = str(metadata.get("language", "unknown"))
        state.repository = state.service if state.service in self.catalog.services else None
        state.source_code_location = metadata.get("source_path")
        state.repository_url = metadata.get("repository_url")
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
            await self._call(state, "list_deployments", {}, "K8s 服务发现", callback)

    async def _baseline(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        system_scan: bool = False,
    ) -> None:
        """硬性采集健康、延迟、错误率、CPU/内存与异常日志。"""
        await self._phase(state, WorkflowPhase.BASELINE_OBSERVATION, callback)
        if system_scan and state.service == "unknown":
            await self._call(
                state,
                "query_metrics",
                {"query": "sum by (service) (up)", "time_range_minutes": state.time_range_minutes},
                "全服务健康基线",
                callback,
            )
            await self._call(
                state,
                "query_metrics",
                {
                    "query": 'sum by (service) (rate(http_server_requests_seconds_count{status=~"5.."}[5m]))',
                    "time_range_minutes": state.time_range_minutes,
                },
                "全服务 HTTP 5xx 速率",
                callback,
            )
            await self._call(
                state,
                "query_metrics",
                {
                    "query": (
                        f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{self.kubernetes_namespace}"}}[5m])) '
                        f'or sum by (pod) (container_memory_working_set_bytes{{namespace="{self.kubernetes_namespace}"}})'
                    ),
                    "time_range_minutes": state.time_range_minutes,
                },
                "全局 Pod CPU/内存",
                callback,
            )
            await self._call(
                state,
                "query_logs",
                {"time_range_minutes": state.time_range_minutes, "level": "error", "limit": 50},
                "全局异常日志",
                callback,
            )
            return
        if state.service == "unknown":
            await self._call(
                state,
                "query_metrics",
                {"query": "sum by (service) (up)", "time_range_minutes": state.time_range_minutes},
                "全服务健康基线",
                callback,
            )
            await self._call(
                state,
                "query_logs",
                {"time_range_minutes": state.time_range_minutes, "limit": 50},
                "跨服务近期日志",
                callback,
            )
            return
        service = state.service
        common = {"service": service, "time_range_minutes": state.time_range_minutes}
        # Pod 清单必须先于聚合指标采集，以便识别单实例异常与混合镜像版本。
        pods = await self._call(state, "list_pods", {"label_selector": f"app={service}"}, "服务 Pod 与运行版本", callback)
        self._extract_pod_runtime(state, pods)
        state.pod_name = state.pod_name or self._find_pod_name(pods, service)
        await self._call(state, "get_service_health", {"service": service}, "服务健康", callback)
        # 优先查询 P95 直方图；服务未开启 histogram 时 INVESTIGATE 仍可依赖日志、Trace 和数据库证据。
        latency = (
            f'histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket'
            f'{{service="{service}"}}[5m])) by (le))'
        )
        await self._call(state, "query_metrics", {"query": latency, "time_range_minutes": state.time_range_minutes}, "HTTP P95", callback)
        errors = f'sum(rate(http_server_requests_seconds_count{{service="{service}",status=~"5.."}}[5m]))'
        await self._call(state, "query_metrics", {"query": errors, "time_range_minutes": state.time_range_minutes}, "HTTP 5xx 速率", callback)
        resources = (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{self.kubernetes_namespace}",pod=~"{service}.*"}}[5m])) '
            f'or sum by (pod) (container_memory_working_set_bytes{{namespace="{self.kubernetes_namespace}",pod=~"{service}.*"}})'
        )
        await self._call(state, "query_metrics", {"query": resources, "time_range_minutes": state.time_range_minutes}, "Pod 级 CPU/内存", callback)
        await self._call(state, "query_logs", {**common, "limit": 20}, "近期服务日志", callback)

    async def _analyze(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """候选原因由后续 Planner 从 Evidence 生成，Workflow 不预置故障答案。"""
        await self._phase(state, WorkflowPhase.ANALYZE, callback)
        state.candidates = []

    async def _investigate(
        self,
        state: DiagnosisState,
        callback: EventCallback | None,
        *,
        planner: EvidencePlanner | None,
        system_scan: bool = False,
    ) -> None:
        """由 Planner 根据当前 Evidence 逐步选择工具，不按 Service/Case 分支。"""
        await self._phase(state, WorkflowPhase.INVESTIGATE, callback)
        del system_scan
        if planner is None or state.service == "unknown":
            return
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
            decision = await planner.decide(state, tool_specs)
            if decision.action != "tool":
                break
            signature = json.dumps(
                [decision.tool_name, decision.arguments], ensure_ascii=False, sort_keys=True
            )
            repeated = any(
                item.error is None
                and json.dumps([item.tool_name, item.arguments], ensure_ascii=False, sort_keys=True) == signature
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
            )
            if decision.tool_name in {"list_pods", "get_pod"}:
                self._extract_pod_runtime(state, result)
            elif decision.tool_name == "get_container_image":
                state.runtime_commit = self._extract_git_sha(result) or state.runtime_commit

    async def _call(
        self,
        state: DiagnosisState,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        callback: EventCallback | None,
        parent_evidence_ids: list[str] | None = None,
    ) -> Any:
        """执行工具并记录 timestamp/duration/error；到达 max_steps 后拒绝继续。"""
        if len(state.timeline) >= self.max_steps:
            return None
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        error: str | None = None
        evidence_id: str | None = None
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
            )
        try:
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
                )
            else:
                evidence_id = f"ev_{uuid4().hex[:16]}"
            summary = normalized.summary
            state.evidence.append(Evidence(
                source=self._source(tool_name), source_type=self._source(tool_name),
                tool_name=tool_name, title=title, detail=summary, summary=summary,
                timestamp=started_at, evidence_id=evidence_id,
                structured_data=normalized.structured_data,
                source_references=references, reference=references,
                parent_evidence_ids=parent_evidence_ids or [],
                next_hints=normalized.next_hints,
                supports_conclusion=self._supports_conclusion(tool_name, summary),
                direct_evidence=self._is_direct_evidence(tool_name, arguments, summary),
            ))
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
                )
        record = ToolCallRecord(
            tool_name=tool_name, arguments=arguments, result_summary=summary,
            timestamp=started_at, duration_ms=int((time.perf_counter() - started) * 1000), error=error,
            evidence_id=evidence_id,
        )
        state.timeline.append(record)
        if callback:
            await callback({"type": "tool", "record": record.model_dump(mode="json")})
        return result

    @staticmethod
    def _exception_text(exc: BaseException) -> str:
        detail = str(exc).strip()
        return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__

    @staticmethod
    def _compact_result(value: Any, limit: int = 900) -> str:
        """报告只展示有界预览；完整 Tool Result 已进入 Conversation Store。"""
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        return text if len(text) <= limit else f"{text[:limit]}…（完整结果见 Conversation Store）"

    @staticmethod
    def _source(tool_name: str) -> str:
        """把 Tool 名归类为独立证据源，供 VERIFY 计算置信度。"""
        if tool_name.startswith("query_metric") or tool_name == "get_service_health":
            return "Prometheus"
        if tool_name == "query_logs":
            return "Loki"
        if tool_name == "query_trace":
            return "Tempo"
        if tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}:
            return "MySQL"
        if tool_name in {"get_repository", "get_current_commit", "get_commit_diff", "list_changed_files"} or tool_name.startswith("get_commit") or tool_name.startswith("read_file") or tool_name.startswith("search_code"):
            return "Git"
        return "Kubernetes"

    @staticmethod
    def _find_pod_name(payload: Any, service: str) -> str | None:
        """从 list_pods 的 bounded 结果中安全提取目标 Pod 名。"""
        try:
            items = payload["data"]["items"]
            return next(item["metadata"]["name"] for item in items if item["metadata"]["name"].startswith(service))
        except (KeyError, TypeError, StopIteration):
            return None

    @staticmethod
    def _extract_git_sha(payload: Any) -> str | None:
        """从 get_container_image 的结构化注解中提取完整运行 SHA。"""
        try:
            annotations = payload["data"]["annotations"]
            value = str(annotations.get("sre.agent/git-sha", ""))
            return value if len(value) == 40 else None
        except (KeyError, TypeError):
            return None

    def _extract_pod_runtime(self, state: DiagnosisState, payload: Any) -> None:
        """比较同一 Service 的 Pod 镜像，优先选择少数版本作为疑似异常实例。"""
        try:
            candidates: list[tuple[str, str]] = []
            for pod in payload["data"]["items"]:
                pod_name = str(pod["metadata"]["name"])
                annotations = pod.get("metadata", {}).get("annotations", {})
                repository = str(annotations.get("sre.agent/repository") or "")
                repository_url = str(annotations.get("sre.agent/repository-url") or "")
                if repository:
                    state.repository = repository
                if annotations.get("sre.agent/source-path"):
                    state.source_code_location = str(annotations["sre.agent/source-path"])
                if annotations.get("sre.agent/language"):
                    state.language = str(annotations["sre.agent/language"])
                if repository_url and state.repository and self.repository_registry:
                    # K8s 运行对象是模块→仓库的事实源；Registry 再执行 URL/主机
                    # 白名单校验，校验通过后 Git MCP 才允许抓取该远程仓库。
                    state.repository_url = self.repository_registry.bind(state.repository, repository_url)
                image = str(pod["spec"]["containers"][0]["image"])
                version = image.rsplit(":", 1)[-1]
                if len(version) == 40:
                    candidates.append((pod_name, version))
            if not candidates:
                return
            state.pod_versions = dict(candidates)
            counts: dict[str, int] = {}
            for _, version in candidates:
                counts[version] = counts.get(version, 0) + 1
            state.mixed_versions = len(counts) > 1
            selected_version = min(counts, key=counts.get) if state.mixed_versions else candidates[0][1]
            state.pod_name = next(pod for pod, version in candidates if version == selected_version)
            state.runtime_commit = selected_version
        except (IndexError, KeyError, TypeError):
            # Pod 尚未 Ready 或返回被截断时保留空状态，后续回退到 Deployment 注解。
            return

    @staticmethod
    def _extract_trace_id(payload: Any) -> str | None:
        """从 Loki 的结构化查询结果中提取最近一条合法 trace_id。

        Loki 返回的日志正文位于 ``data.result[].values[][1]``，正文自身是 JSON
        字符串。这里逐条解析而不使用字符串切片，避免普通消息文本里偶然出现
        ``trace_id`` 字样时被误认为真正的链路标识。
        """
        try:
            # ``bounded`` 在外层增加 data，工具本身又使用 result 包装 Loki 的
            # data，因此真实 streams 位于 data.result.result。显式逐层取值也能
            # 在上游协议变化时快速降级，而不会让整个诊断接口返回 500。
            streams = payload["data"]["result"]["result"]
            for stream in streams:
                for value in reversed(stream.get("values", [])):
                    if not isinstance(value, list) or len(value) < 2:
                        continue
                    log_record = json.loads(value[1])
                    trace_id = str(log_record.get("trace_id", ""))
                    if len(trace_id) in {16, 32} and all(char in "0123456789abcdefABCDEF" for char in trace_id):
                        return trace_id
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
            # 日志结构不完整时返回 None，由调用方安全降级为按 service 搜索 Trace。
            return None
        return None

    @staticmethod
    def _supports_conclusion(tool_name: str, summary: str) -> bool:
        """防止空查询或零行结果被计入双证据门槛。"""
        if tool_name.startswith("query_") or tool_name == "get_service_health":
            empty_markers = ('"result": []', '"traces": []', '"row_count": 0')
            return not any(marker in summary for marker in empty_markers)
        return True

    @staticmethod
    def _is_direct_evidence(tool_name: str, arguments: dict[str, Any], summary: str) -> bool:
        """标记能够直接观测故障机制的数据，代码和服务清单仅作旁证。"""
        if not DiagnosisWorkflow._supports_conclusion(tool_name, summary):
            return False
        if tool_name == "query_trace":
            return bool(arguments.get("trace_id"))
        return tool_name in {
            "query_metrics", "get_service_health", "query_logs", "query_slow_queries",
            "query_sql_digest", "explain_sql", "get_pod", "get_pod_events",
            "get_restart_count", "get_deployment", "get_container_image",
        }

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
        self.conversation_service.append(
            state.user_id,
            state.conversation_id,
            "assistant",
            {"report": report.model_dump(mode="json")},
            message_type="assistant",
            run_id=state.run_id,
        )
        # 报告持久化后立即允许 SSE 返回 final。压缩是增强任务，即使本地模型
        # 冷启动或生成缓慢，也不能让已经完成的诊断在页面上继续卡数分钟。
        task = asyncio.create_task(
            self._compact_after_report(state.user_id, state.conversation_id),
            name=f"compact-{state.conversation_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _compact_after_report(self, user_id: str, conversation_id: str) -> None:
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
        if self.context_service is None or not state.user_id:
            return
        try:
            await asyncio.wait_for(
                self.context_service.maybe_compact(state.user_id, state.conversation_id),
                timeout=30,
            )
        except (GatewayError, ValueError, TimeoutError):
            return

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
        """Evidence Gate 验证引用、直接证据和矛盾后才允许 confirmed。"""
        synthesis = state.synthesis or DiagnosisSynthesis(
            status="insufficient_evidence",
            root_cause="证据不足，无法确认根因",
            confidence=0.0,
        )
        evidence_by_id = {item.evidence_id: item for item in state.evidence}
        cited_ids = list(dict.fromkeys(
            evidence_id for evidence_id in synthesis.evidence_ids if evidence_id in evidence_by_id
        ))
        cited = [evidence_by_id[evidence_id] for evidence_id in cited_ids]
        gate_passed = (
            synthesis.status == "confirmed"
            and len(cited) >= 2
            and any(item.direct_evidence and item.supports_conclusion for item in cited)
            and all(item.supports_conclusion for item in cited)
            and not synthesis.contradictions
        )
        status = "confirmed" if gate_passed else "insufficient_evidence"
        root = synthesis.root_cause.strip() or "证据不足，无法确认根因"
        confidence = synthesis.confidence if gate_passed else min(synthesis.confidence, 0.49)
        conclusion = f"已确认：{root}" if gate_passed else f"证据不足：{root}"
        findings = [DiagnosisFinding(finding=root, evidence_ids=cited_ids)] if gate_passed else []
        chain = synthesis.root_cause_chain or ["当前观测证据", "尚不足以形成可验证根因"]
        fixes = synthesis.recommended_fix or ["补充与候选机制直接相关的 Metrics、Logs、Trace 或运行时状态后重新诊断"]
        return DiagnosisReport(
            query=state.query, run_id=state.run_id, service=state.service, affected_pod=state.pod_name,
            language=state.language, running_version=state.runtime_commit, git_sha=state.runtime_commit,
            source_code_location=state.source_code_location, repository_url=state.repository_url,
            symptom=state.symptom,
            environment=state.environment, time_range=f"最近 {state.time_range_minutes} 分钟",
            conclusion=conclusion,
            status=status,
            decision_summary=conclusion,
            root_cause=root, findings=findings, evidence=state.evidence,
            root_cause_chain=chain, recommended_fix=fixes, confidence=confidence,
            token_usage=state.prompt_tokens + state.completion_tokens,
            candidates=state.candidates, investigation_timeline=state.timeline,
            workflow_phases=state.phases,
            context_compaction={
                "strategy": "mysql-conversation-compaction-v1",
                "storage": "mysql",
                "stored_evidence": len(state.evidence),
            },
        )
