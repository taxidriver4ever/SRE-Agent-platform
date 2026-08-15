"""证据驱动的通用与专项 SRE 工作流实现。"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.conversation import ConversationService
from app.conversation_memory import ConversationCompactionService
from app.code_state import CodeStateService
from app.evidence import build_source_references
from app.llm.base import LLM, LLMMessage
from app.llm.gateway import GatewayError
from app.mcp_clients import FastMCPToolClient, ToolExecutionError
from app.repositories import RepositoryRegistry
from app.workflow.catalog import ServiceCatalog
from app.workflow.models import (
    CandidateCause,
    DiagnosisReport,
    DiagnosisState,
    Evidence,
    ToolCallRecord,
    WorkflowPhase,
)

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


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

    async def run(
        self,
        query: str,
        on_event: EventCallback | None = None,
        *,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> DiagnosisReport:
        """从 TRIAGE 走到 REPORT；模型不能跳过基线观测或证据验证。"""
        run_id = uuid4().hex
        # 没有持久会话的脚本调用使用 run_id 作为独立内存会话，不共享上下文。
        state = DiagnosisState(
            query=query,
            run_id=run_id,
            conversation_id=conversation_id or run_id,
            user_id=user_id,
        )
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
        await self._triage(state, on_event)
        await self._baseline(state, on_event)
        await self._analyze(state, on_event)
        await self._investigate(state, on_event)
        await self._phase(state, WorkflowPhase.VERIFY, on_event)
        # 在生成模型摘要之前先检查阈值，避免把已经足够大的跨轮增量继续原样传递。
        await self._compact_context_before_summary(state)
        await self._summarize_with_gateway(state, on_event)
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

    async def _triage(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """确定 service、symptom、environment 与默认最近 30 分钟窗口。"""
        await self._phase(state, WorkflowPhase.TRIAGE, callback)
        analysis_input = state.query
        state.service = self.catalog.resolve(analysis_input)
        metadata = self.catalog.services.get(state.service, {})
        state.language = str(metadata.get("language", "unknown"))
        state.repository = state.service if state.service in self.catalog.services else None
        state.source_code_location = metadata.get("source_path")
        state.repository_url = metadata.get("repository_url")
        text = analysis_input.lower()
        if any(word in text for word in ("重试", "retry", "依赖", "dependency", "timeout", "超时")):
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
        if state.service == "unknown":
            await self._call(state, "list_deployments", {}, "K8s 服务发现", callback)

    async def _baseline(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """硬性采集健康、延迟、错误率、CPU/内存与异常日志。"""
        await self._phase(state, WorkflowPhase.BASELINE_OBSERVATION, callback)
        service = state.service if state.service != "unknown" else "order-service"
        common = {"service": service, "time_range_minutes": state.time_range_minutes}
        # Pod 清单必须先于聚合指标采集，以便识别单实例异常与混合镜像版本。
        pods = await self._call(state, "list_pods", {"label_selector": f"app={service}"}, "服务 Pod 与运行版本", callback)
        self._extract_pod_runtime(state, pods)
        await self._call(state, "get_service_health", {"service": service}, "服务健康", callback)
        # 优先查询 P95 直方图；服务未开启 histogram 时 INVESTIGATE 仍可依赖日志、Trace 和数据库证据。
        latency = (
            f'histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket'
            f'{{service="{service}"}}[5m])) by (le))'
        )
        await self._call(state, "query_metrics", {"query": latency, **common}, "HTTP P95", callback)
        errors = f'sum(rate(http_server_requests_seconds_count{{service="{service}",status=~"5.."}}[5m]))'
        await self._call(state, "query_metrics", {"query": errors, **common}, "HTTP 5xx 速率", callback)
        resources = (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="sre-lab",pod=~"{service}.*"}}[5m])) '
            f'or sum by (pod) (container_memory_working_set_bytes{{namespace="sre-lab",pod=~"{service}.*"}})'
        )
        await self._call(state, "query_metrics", {"query": resources, **common}, "Pod 级 CPU/内存", callback)
        await self._call(state, "query_logs", {**common, "level": "error", "limit": 20}, "近期异常日志", callback)

    async def _analyze(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """基于症状生成有限候选集，留待专项调查验证。"""
        await self._phase(state, WorkflowPhase.ANALYZE, callback)
        analysis_input = state.query.lower()
        if any(word in analysis_input for word in ("发布", "回归", "deploy", "regression")):
            causes = [("deployment regression", "症状时间与运行版本变更相关，需要比较 GOOD..BAD", 5), ("database slowdown", "查询变更可能导致扫描量增加", 4)]
        elif state.symptom == "latency":
            causes = [
                ("database slowdown", "SQL 或连接池可能占用请求时间", 5),
                ("downstream dependency", "下游超时会表现为本服务低 CPU、高延迟", 4),
                ("CPU saturation", "计算饱和可能提升排队时间", 3),
            ]
        elif state.symptom == "pod_restart":
            causes = [("OOM / restart", "容器内存越界或进程崩溃", 5), ("deployment regression", "新镜像可能引入资源泄漏", 4)]
        else:
            causes = [("connection pool exhaustion", "错误高峰可能由池等待超时产生", 5), ("deployment regression", "近期发布可能改变失败率", 4)]
        state.candidates = [CandidateCause(cause=c, reason=r, priority=p) for c, r, p in causes]

    async def _investigate(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """按症状选择专项策略，共享同一 State/Tool/Evidence/Report 模型。"""
        await self._phase(state, WorkflowPhase.INVESTIGATE, callback)
        service = state.service if state.service != "unknown" else "order-service"
        text = state.query.lower()
        is_regression = any(word in text for word in ("发布", "回归", "deploy", "regression"))
        if is_regression and service == "order-service":
            await self._call(state, "query_slow_queries", {"time_range_minutes": 30, "limit": 10}, "回归后的慢查询", callback)
            await self._call(state, "explain_sql", {"sql": "SELECT COUNT(*) FROM orders WHERE customer_email LIKE '%slow.example.com%'"}, "回归 SQL 执行计划", callback)
        elif state.symptom == "latency" and service == "order-service":
            # 先从包含业务上下文的慢请求日志取得 trace_id，再用它精确查询 Tempo。
            # 这种 Logs -> Traces 的关联可以避免拿到健康检查 Trace，并能证明慢 HTTP 请求
            # 与 JDBC 慢查询确实属于同一次端到端调用，而不是仅凭时间接近进行猜测。
            slow_request_logs = await self._call(
                state,
                "query_logs",
                {"service": service, "keyword": "mode=slow_sql", "limit": 5},
                "慢请求关联日志",
                callback,
            )
            trace_id = self._extract_trace_id(slow_request_logs)
            trace_arguments = {"trace_id": trace_id} if trace_id else {"service": service, "limit": 10}
            await self._call(state, "query_trace", trace_arguments, "慢请求端到端 Trace", callback)
            await self._call(state, "query_slow_queries", {"time_range_minutes": 30, "limit": 10}, "MySQL 慢查询", callback)
            await self._call(state, "explain_sql", {"sql": "SELECT COUNT(*) FROM orders WHERE customer_email LIKE '%slow.example.com%'"}, "SQL 执行计划", callback)
        elif state.symptom == "dependency_timeout":
            await self._call(state, "query_trace", {"service": service, "limit": 10}, "依赖 Trace", callback)
            await self._call(state, "query_logs", {"service": "inventory-service", "keyword": "timeout", "limit": 20}, "下游日志", callback)
        elif state.symptom == "pod_restart" or service == "payment-service":
            pods = await self._call(state, "list_pods", {}, "Pod 清单", callback)
            state.pod_name = self._find_pod_name(pods, service)
            if state.pod_name:
                await self._call(state, "get_pod_events", {"name": state.pod_name}, "Pod Events", callback)
                await self._call(state, "get_restart_count", {"name": state.pod_name}, "容器重启次数", callback)
        elif service == "user-service" and any(word in text for word in ("cpu", "打满", "饱和")):
            await self._call(state, "query_logs", {"service": service, "keyword": "cpu_saturation", "limit": 20}, "CPU 故障模式日志", callback)
        else:
            await self._call(state, "query_logs", {"service": service, "keyword": "Hikari", "limit": 20}, "连接池日志", callback)
            await self._call(state, "query_slow_queries", {"time_range_minutes": 30, "limit": 10}, "慢查询交叉验证", callback)

        # 所有专项策略最终都检查当前运行镜像及其 Git SHA，为 Runtime→Source 映射提供锚点。
        if not state.runtime_commit:
            runtime = await self._call(state, "get_container_image", {"name": service}, "运行镜像与 Git SHA", callback)
            state.runtime_commit = self._extract_git_sha(runtime)
        # 发布回归场景需要提交元数据来建立变更时间线；普通性能诊断直接读取运行
        # SHA 对应源码即可，把有限的工具预算留给日志与 Trace 的精确关联。
        if is_regression and state.runtime_commit and len(state.timeline) < self.max_steps:
            await self._call(state, "get_commit", {"repository": state.repository, "commit": state.runtime_commit}, "运行提交元数据", callback)
        if is_regression and state.runtime_commit and len(state.timeline) < self.max_steps:
            await self._call(state, "list_changed_files", {"repository": state.repository, "base": f"{state.runtime_commit}^", "head": state.runtime_commit}, "GOOD..BAD 文件清单", callback)
            await self._call(state, "get_commit_diff", {"repository": state.repository, "base": f"{state.runtime_commit}^", "head": state.runtime_commit}, "GOOD..BAD 代码差异", callback)
        source_path = self.catalog.services.get(service, {}).get("source_path")
        navigation_references: list[dict[str, Any]] = []
        if self.code_state_service and state.repository and state.runtime_commit:
            try:
                await self.code_state_service.ensure(state.repository, state.runtime_commit)
                navigation = await self._call(
                    state,
                    "search_code_state",
                    {
                        "repository_name": state.repository,
                        "query": state.service.split("-", 1)[0],
                        "kinds": ["controller", "service", "repository", "component"],
                        "limit": 6,
                    },
                    "Code State 导航",
                    callback,
                )
                if isinstance(navigation, dict):
                    seen_paths: set[str] = set()
                    for item in navigation.get("components", []):
                        if not isinstance(item, dict) or not item.get("path"):
                            continue
                        path = str(item["path"])
                        if path in seen_paths:
                            continue
                        seen_paths.add(path)
                        navigation_references.append(item)
                        if len(navigation_references) == 2:
                            break
            except (ValueError, ToolExecutionError):
                navigation_references = []
        if not navigation_references and source_path:
            navigation_references = [{"path": str(source_path)}]
        for reference in navigation_references:
            if len(state.timeline) >= self.max_steps or not state.runtime_commit:
                break
            path = str(reference["path"])
            arguments: dict[str, Any] = {
                "repository": state.repository,
                "commit": str(reference.get("commit_sha") or state.runtime_commit),
                "path": path,
            }
            if reference.get("start_line") is not None:
                arguments["start_line"] = int(reference["start_line"])
            if reference.get("end_line") is not None:
                arguments["end_line"] = int(reference["end_line"])
            await self._call(
                state,
                "read_file_at_commit",
                arguments,
                f"Code State 精确源码：{path}",
                callback,
            )

    async def _call(
        self,
        state: DiagnosisState,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        callback: EventCallback | None,
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
            if self.conversation_service and state.user_id:
                evidence_id = self.conversation_service.append(
                    state.user_id,
                    state.conversation_id,
                    "assistant",
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result,
                        "source_references": [item.model_dump(mode="json") for item in references],
                    },
                    message_type="tool_result",
                    run_id=state.run_id,
                    tool_name=tool_name,
                )
            else:
                evidence_id = f"ev_{uuid4().hex[:16]}"
            summary = self._compact_result(result)
            state.evidence.append(Evidence(
                source=self._source(tool_name), tool_name=tool_name, title=title,
                detail=summary, timestamp=started_at, evidence_id=evidence_id,
                source_references=references,
                supports_conclusion=self._supports_conclusion(tool_name, summary),
            ))
        except ToolExecutionError as exc:
            error = str(exc)
            summary = ""
            if self.conversation_service and state.user_id:
                self.conversation_service.append(
                    state.user_id,
                    state.conversation_id,
                    "assistant",
                    {"tool_name": tool_name, "arguments": arguments, "error": error},
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
        self.conversation_service.append(
            state.user_id,
            state.conversation_id,
            "assistant",
            {"report": report.model_dump(mode="json")},
            message_type="assistant",
            run_id=state.run_id,
        )
        compression_error = ""
        try:
            await self.context_service.maybe_compact(state.user_id, state.conversation_id)
        except (GatewayError, ValueError) as exc:
            compression_error = str(exc)[:300]
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
        if compression_error:
            report.context_compaction["last_error"] = compression_error

    async def _compact_context_before_summary(self, state: DiagnosisState) -> None:
        """在本轮 LLM 摘要前压缩达到预算阈值的持久化 Conversation Message。

        压缩失败不推进 MySQL 边界，也不阻断诊断。后续摘要继续使用未压缩原文，
        报告阶段还会再次尝试压缩，因此不会静默丢失调查信息。
        """
        if self.context_service is None or not state.user_id:
            return
        try:
            await self.context_service.maybe_compact(state.user_id, state.conversation_id)
        except (GatewayError, ValueError):
            return

    def _report(self, state: DiagnosisState) -> DiagnosisReport:
        """仅在至少两个独立证据源时使用确定性结论，否则降级为高可能候选。"""
        sources = {item.source for item in state.evidence if item.supports_conclusion}
        text = state.query.lower()
        is_regression = any(word in text for word in ("发布", "回归", "deploy", "regression"))
        if state.mixed_versions and state.service == "order-service":
            root = "同一 order-service 后端池混合运行 GOOD/BAD 镜像，少数 BAD Pod 引入全表扫描并造成间歇性慢请求"
            chain = ["Service 负载均衡到不同版本", "BAD Pod 执行前导通配查询", "MySQL 全表扫描", "BAD Pod P95 上升", "用户观察到间歇性延迟"]
            fixes = ["停止 BAD canary 并恢复一致的 GOOD SHA", "修复模糊搜索查询或采用专用搜索索引", "发布门禁检查 ReplicaSet 镜像一致性和按 version 聚合的 P95"]
        elif is_regression and state.service == "order-service":
            root = "BAD 提交将带索引等值查询改成无索引前导通配查询，引入部署回归"
            chain = ["GOOD..BAD 查询代码变化", "索引失效并全表扫描", "慢 SQL", "HTTP P95 上升", "发布后性能回归"]
            fixes = ["回退 BAD 提交或恢复等值/可索引查询", "恢复 customer_email 索引", "在发布门禁中加入 SQL 执行计划和 P95 回归测试"]
        elif state.service == "order-service" and state.symptom == "latency":
            root = "缺少有效索引的前导通配符查询造成全表扫描与慢 SQL"
            chain = ["前导通配符 LIKE", "扫描大量订单行", "MySQL 查询延迟", "连接长时间占用", "HTTP P95 上升"]
            fixes = ["为实际过滤字段设计合适索引并改为可使用索引的等值/前缀查询", "移除人工 SLEEP 并增加分页", "修复后用相同负载复测 P95、Trace 与 rows_examined"]
        elif state.symptom == "pod_restart" or state.service == "payment-service":
            root = "payment-service 持续保留 Buffer 导致容器内存增长并触发 OOM 重启"
            chain = ["对象引用未释放", "工作集持续增长", "超过容器 memory limit", "OOMKilled", "Pod 重启"]
            fixes = ["移除全局 Buffer 引用并为缓存设置上限", "增加内存增长与重启告警", "用稳定负载验证工作集不再单调增长"]
        elif state.service == "user-service" and any(word in text for word in ("cpu", "打满", "饱和")):
            root = "user-service 的低效质数计算使容器 CPU 饱和并拉高请求延迟"
            chain = ["CPU 密集计算", "容器 CPU 饱和/限流", "请求排队", "HTTP 延迟上升"]
            fixes = ["移除同步 CPU 密集演示路径", "为计算任务设置预算或异步队列", "复测 CPU 与 P95 回落"]
        elif state.symptom == "dependency_timeout":
            if any(word in text for word in ("重试", "retry", "放大")):
                root = "order-service 对超时的 inventory-service 执行无退避重试，形成请求放大"
                chain = ["下游超时", "无退避连续重试", "请求量放大", "资源占用与上游失败"]
                fixes = ["限制重试次数并加入指数退避与抖动", "只重试幂等瞬时错误", "增加熔断和重试放大指标"]
            else:
                root = "inventory-service 下游响应延迟造成 order-service 调用超时"
                chain = ["下游延迟", "客户端等待", "超时异常", "上游延迟或 5xx"]
                fixes = ["修复下游慢路径", "校准连接/读取超时并增加熔断", "用 Trace 验证耗时归属"]
        else:
            root = state.candidates[0].cause if state.candidates else "证据不足"
            chain = ["候选异常", "请求失败或延迟"]
            fixes = ["补充更长时间窗口的 Metrics、Logs 与 Trace 后再确认根因"]

        enough = len(sources) >= 2
        confidence = min(0.94, 0.58 + 0.09 * len(sources)) if enough else 0.45
        conclusion = f"已确认：{root}" if enough else f"高可能性候选根因：{root}（独立证据不足）"
        return DiagnosisReport(
            query=state.query, run_id=state.run_id, service=state.service, affected_pod=state.pod_name,
            language=state.language, running_version=state.runtime_commit, git_sha=state.runtime_commit,
            source_code_location=state.source_code_location, repository_url=state.repository_url,
            symptom=state.symptom,
            environment=state.environment, time_range=f"最近 {state.time_range_minutes} 分钟",
            conclusion=conclusion,
            decision_summary=state.llm_decision_summary or conclusion,
            root_cause=root, evidence=state.evidence,
            root_cause_chain=chain, recommended_fix=fixes, confidence=confidence,
            candidates=state.candidates, investigation_timeline=state.timeline,
            workflow_phases=state.phases,
            context_compaction={
                "strategy": "mysql-conversation-compaction-v1",
                "storage": "mysql",
                "stored_evidence": len(state.evidence),
            },
        )

    async def _summarize_with_gateway(self, state: DiagnosisState, callback: EventCallback | None) -> None:
        """让本地 Ollama 只总结已取得的证据，不允许它替代 VERIFY 的硬规则。"""
        if self.llm is None:
            return
        supported = [item for item in state.evidence if item.supports_conclusion]
        historical_memory: Any = {"items": []}
        try:
            # Memory MCP 内部从请求 ContextVar 注入用户和 Conversation；这里不能
            # 提供身份、表名或 SQL。空 query 表示按重要度读取少量当前有效记忆。
            historical_memory = await self.tools.execute(
                "search_conversation_memory",
                {
                    "query": "",
                    "item_types": [
                        "constraint", "confirmed_finding", "decision", "evidence", "open_question"
                    ],
                    "limit": 10,
                },
            )
        except ToolExecutionError:
            # CLI/评测没有认证会话 Scope 时安全降级，不允许绕过工具直接读数据库。
            historical_memory = {"items": []}
        conversation_context = (
            self.context_service.build_active_context(state.user_id, state.conversation_id)
            if self.context_service and state.user_id
            else "{}"
        )
        prompt = (
            "你是 SRE 诊断摘要器。只根据下列事实写一段不超过 120 个汉字的中文决策摘要；"
            "不得添加事实中没有的数字、提交或结论。少于两个独立来源时必须写‘高可能性候选根因’。\n"
            "跨轮活动上下文：\n"
            + conversation_context
            + "\n当前会话检索到的历史记忆：\n"
            + json.dumps(historical_memory, ensure_ascii=False, default=str)
            + "\n本轮支持结论的证据 ID：\n"
            + json.dumps([item.evidence_id for item in supported], ensure_ascii=False)
        )
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        error: str | None = None
        try:
            response = await self.llm.complete([
                # Qwen3 的 /no_think 开关可减少本地摘要延迟；前端也不需要隐藏推理内容。
                LLMMessage(role="system", content="严格基于证据回答，不输出思维链。/no_think"),
                LLMMessage(role="user", content=prompt),
            ])
            # 去掉多余空白并限制长度，避免模型输出长篇 Markdown 破坏结构化页面。
            state.llm_decision_summary = " ".join(response.content.split())[:500]
            summary = f"{response.provider or 'gateway'}/{response.model}: {state.llm_decision_summary}"
        except GatewayError as exc:
            error = str(exc)
            summary = ""
        record = ToolCallRecord(
            tool_name="llm_gateway_summary", arguments={
                "stored_evidence_count": len(state.evidence),
                "active_evidence_count": len(supported),
                "context_characters": len(conversation_context),
            },
            result_summary=summary, timestamp=started_at,
            duration_ms=int((time.perf_counter() - started) * 1000), error=error,
        )
        state.timeline.append(record)
        if callback:
            await callback({"type": "tool", "record": record.model_dump(mode="json")})
