"""把现有真实只读 DiagnosisWorkflow 投影为 Incident/Diagnosis Workspace。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.diagnosis.models import (
    DiagnosisEvidence, DiagnosisRootCause, DiagnosisStatus, DiagnosisTargetType,
    IncidentGraph, IncidentGraphEdge, IncidentGraphNode, RootCauseResource,
)
from app.diagnosis.repository import DiagnosisRepository
from app.diagnosis.schemas import DiagnosisCreateRequest
from app.diagnosis.service import DiagnosisService
from app.workflow import DiagnosisReport, DiagnosisWorkflow


class DiagnosisOrchestrator:
    """复用真实工具编排，并只保存外部可验证步骤，不暴露隐藏推理。"""

    def __init__(
        self,
        workflow: DiagnosisWorkflow,
        service: DiagnosisService,
        repository: DiagnosisRepository,
    ) -> None:
        self.workflow = workflow
        self.service = service
        self.repository = repository

    async def run(self, user_id: str, diagnosis_id: str, request: DiagnosisCreateRequest) -> DiagnosisReport:
        session = self.service.transition(user_id, diagnosis_id, DiagnosisStatus.INVESTIGATING)
        target, system_scan = self._resolve_target(request)
        target_type = request.initial_target.type.value if request.initial_target else None
        target_id = request.initial_target.name if request.initial_target else (target if target != "unknown" else None)
        self.repository.append_step(
            diagnosis_id, step_type="TARGET_RESOLUTION", status="COMPLETED",
            target_type=target_type or ("SERVICE" if target_id else None), target_id=target_id,
            summary=self._target_summary(request, target),
        )
        self.repository.append_event(diagnosis_id, "diagnosis.started", {
            "diagnosis_id": diagnosis_id, "status": DiagnosisStatus.INVESTIGATING.value,
            "resolved_target": target_id, "system_scan": system_scan,
        })

        async def publish(event: dict[str, Any]) -> None:
            event_type = str(event.get("type", ""))
            if event_type == "phase":
                self.repository.append_event(diagnosis_id, "phase.changed", {
                    "phase": event.get("phase"),
                })
            elif event_type == "tool" and isinstance(event.get("record"), dict):
                self._persist_tool_step(diagnosis_id, event["record"], target_id)

        report = await self.workflow.run(
            request.question,
            publish,
            conversation_id=session.conversation_id,
            user_id=user_id,
            target=target if target != "unknown" else None,
            symptom=request.question,
            system_scan=system_scan,
        )
        self._persist_report(diagnosis_id, report, request)
        return report

    def resolve_target(self, request: DiagnosisCreateRequest) -> tuple[str, bool]:
        """公开一次性诊断所需的目标解析，但不创建或读取会话。"""
        return self._resolve_target(request)

    def build_quick_result(
        self, report: DiagnosisReport, request: DiagnosisCreateRequest,
    ) -> dict[str, Any]:
        """把工作流报告投影为前端可直接展示的因果链、拓扑和根因。"""
        graph, affected = self._build_graph(report, request)
        root = self._build_root_cause(report, graph)
        return {
            "report": report.model_dump(mode="json"),
            "graph": graph.model_dump(mode="json"),
            "root_cause": root.model_dump(mode="json"),
            "affected_services": affected,
        }

    def _resolve_target(self, request: DiagnosisCreateRequest) -> tuple[str, bool]:
        if request.initial_target is None:
            resolved = self.workflow.catalog.resolve(request.question)
            return resolved, resolved == "unknown"
        if request.initial_target.type is DiagnosisTargetType.SERVICE:
            return request.initial_target.name, False
        # Pod 是初始调查对象，不是调查边界。目录匹配不到时，按常见
        # Deployment Pod 命名从最长服务名前缀解析所属服务。
        pod_name = request.initial_target.name
        matches = [name for name in self.workflow.catalog.services if pod_name.startswith(f"{name}-")]
        return max(matches, key=len) if matches else self.workflow.catalog.resolve(pod_name), not bool(matches)

    def _persist_tool_step(self, diagnosis_id: str, record: dict[str, Any], fallback_target: str | None) -> None:
        arguments = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
        target_type, target_id = self._tool_target(arguments, fallback_target)
        error = str(record.get("error") or "").strip() or None
        evidence_ids = [str(record["evidence_id"])] if record.get("evidence_id") else []
        step = self.repository.append_step(
            diagnosis_id, step_type="TOOL", tool_name=str(record.get("tool_name") or "unknown"),
            target_type=target_type, target_id=target_id,
            status="FAILED" if error else "COMPLETED",
            started_at=str(record.get("timestamp") or "") or None,
            summary=str(record.get("result_summary") or error or "工具未返回摘要"),
            evidence_ids=evidence_ids, error_message=error,
        )
        self.repository.append_event(diagnosis_id, "step.started", {
            "id": step.id, "diagnosis_id": diagnosis_id,
            "sequence_no": step.sequence_no, "tool_name": step.tool_name,
            "target_type": step.target_type, "target_id": step.target_id,
            "started_at": step.started_at,
        })
        self.repository.append_event(
            diagnosis_id, "step.failed" if error else "step.completed",
            step.model_dump(mode="json"),
        )

    def _persist_report(
        self,
        diagnosis_id: str,
        report: DiagnosisReport,
        request: DiagnosisCreateRequest,
    ) -> None:
        for item in report.evidence:
            references = [reference.model_dump(mode="json") for reference in item.source_references]
            evidence = DiagnosisEvidence(
                id=item.evidence_id, diagnosis_id=diagnosis_id,
                source_type=(item.source_type or item.source or "TOOL").upper(),
                source_name=item.tool_name,
                resource_type=self._evidence_resource_type(item.tool_name),
                resource_id=self._evidence_resource_id(item.structured_data, report.service),
                title=item.title, summary=item.summary or item.detail,
                raw_data={
                    "storage": "conversation_tool_result",
                    "reference": f"/api/agent/evidence/{report.run_id}/{item.evidence_id}",
                    "structured_data": item.structured_data,
                },
                metadata={
                    "source_references": references,
                    "parent_evidence_ids": item.parent_evidence_ids,
                    "next_hints": item.next_hints,
                    "direct_evidence": item.direct_evidence,
                    "raw_result_url": f"/api/agent/evidence/{report.run_id}/{item.evidence_id}",
                },
                supports_conclusion=item.supports_conclusion,
                timestamp=item.timestamp.isoformat(),
            )
            self.repository.upsert_evidence(evidence)
            self.repository.append_event(diagnosis_id, "evidence.created", {
                "evidence_id": evidence.id, "source_type": evidence.source_type,
                "resource_id": evidence.resource_id, "title": evidence.title,
            })

        graph, affected = self._build_graph(report, request)
        root = self._build_root_cause(report, graph)
        self.repository.replace_graph(diagnosis_id, graph)
        self.repository.upsert_root_cause(diagnosis_id, root)
        self.repository.append_step(
            diagnosis_id, step_type="REPORT", status="COMPLETED",
            target_type=root.root_resource.type if root.root_resource else None,
            target_id=root.root_resource.name if root.root_resource else None,
            summary=report.decision_summary, evidence_ids=root.evidence_ids,
        )
        self.repository.update_session(
            diagnosis_id, status=DiagnosisStatus.COMPLETED, run_id=report.run_id,
            summary=report.decision_summary, affected_services=affected, finished=True,
        )
        self.repository.append_event(diagnosis_id, "graph.updated", graph.model_dump(mode="json"))
        for node in graph.nodes:
            self.repository.append_event(diagnosis_id, "resource.discovered", {
                "resource": node.model_dump(mode="json"),
            })
        self.repository.append_event(diagnosis_id, "root_cause.generated", root.model_dump(mode="json"))
        self.repository.append_event(diagnosis_id, "diagnosis.completed", {
            "diagnosis_id": diagnosis_id, "status": DiagnosisStatus.COMPLETED.value,
            "summary": report.decision_summary, "affected_services": affected,
        })

    def _build_graph(
        self,
        report: DiagnosisReport,
        request: DiagnosisCreateRequest,
    ) -> tuple[IncidentGraph, list[str]]:
        catalog = self.workflow.catalog.services
        services: set[str] = set()
        if report.service in catalog:
            services.add(report.service)
        for evidence in report.evidence:
            services.update(str(item) for item in evidence.structured_data.get("services", []) if str(item) in catalog)
            services.update(
                str(item.get("service")) for item in evidence.structured_data.get("dependency_candidates", [])
                if isinstance(item, dict) and str(item.get("service")) in catalog
            )
        if request.initial_target and request.initial_target.type is DiagnosisTargetType.SERVICE:
            services.add(request.initial_target.name)
        # 目录中的依赖只在已经发现一端时展开，避免把整张静态拓扑都误标为受影响。
        queue = list(services)
        while queue and len(services) < 20:
            current = queue.pop(0)
            for dependency in catalog.get(current, {}).get("dependencies", []):
                dependency = str(dependency)
                if dependency in catalog and dependency not in services:
                    services.add(dependency)
                    queue.append(dependency)

        nodes = [IncidentGraphNode(
            id=f"service:{name}", type="SERVICE", name=name,
            status="AFFECTED" if name == report.service or name in services else "UNKNOWN",
            metadata={"language": catalog.get(name, {}).get("language", "unknown")},
        ) for name in sorted(services)]
        edges: list[IncidentGraphEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()
        for source in sorted(services):
            for dependency in catalog.get(source, {}).get("dependencies", []):
                dependency = str(dependency)
                if dependency in services:
                    self._add_edge(edges, edge_keys, source, dependency, "DEPENDS_ON")

        for evidence in report.evidence:
            for candidate in evidence.structured_data.get("dependency_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                target = str(candidate.get("service") or "")
                if report.service in services and target in services:
                    self._add_edge(
                        edges, edge_keys, report.service, target, "HTTP",
                        float(candidate.get("duration_ms") or 0), [evidence.evidence_id],
                    )

        database_evidence = [
            evidence for evidence in report.evidence
            if evidence.tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}
        ]
        if database_evidence:
            database_name = "mysql"
            nodes.append(IncidentGraphNode(
                id=f"database:{database_name}", type="DATABASE", name=database_name,
                status="AFFECTED", metadata={"source": "MYSQL"},
            ))
            source = report.service if report.service in services else (sorted(services)[0] if services else None)
            if source:
                evidence_ids = [evidence.evidence_id for evidence in database_evidence]
                edges.append(IncidentGraphEdge(
                    id=uuid4().hex, source=f"service:{source}", target=f"database:{database_name}",
                    relation="SQL", status="AFFECTED", evidence_ids=evidence_ids,
                ))

        pod = request.initial_target.name if (
            request.initial_target and request.initial_target.type is DiagnosisTargetType.POD
        ) else report.affected_pod
        if pod:
            nodes.append(IncidentGraphNode(
                id=f"pod:{pod}", type="POD", name=pod, status="AFFECTED",
                metadata={"namespace": request.initial_target.namespace},
            ))
            owner = report.service if report.service in services else None
            if owner:
                edge_id = uuid4().hex
                edges.append(IncidentGraphEdge(
                    id=edge_id, source=f"service:{owner}", target=f"pod:{pod}",
                    relation="RUNS_ON", status="AFFECTED",
                ))

        return IncidentGraph(nodes=nodes, edges=edges), sorted(services)

    def _build_root_cause(self, report: DiagnosisReport, graph: IncidentGraph) -> DiagnosisRootCause:
        combined = " ".join([report.root_cause, report.conclusion, *report.root_cause_chain]).lower()
        database = next((node for node in graph.nodes if node.type == "DATABASE"), None)
        database_markers = ("mysql", "database", "数据库", "慢查询", "索引", "sql")
        candidates = sorted(
            (node for node in graph.nodes if node.type == "SERVICE" and node.name.lower() in combined),
            key=lambda node: len(node.name), reverse=True,
        )
        if database and any(marker in combined for marker in database_markers):
            root_resource = RootCauseResource(type="DATABASE", name=database.name)
        else:
            root_resource = RootCauseResource(type="SERVICE", name=candidates[0].name) if candidates else None
        if root_resource:
            for node in graph.nodes:
                if node.name == root_resource.name:
                    node.status = "ROOT_CAUSE"
                    break
        evidence_ids = list(dict.fromkeys(
            evidence_id for finding in report.findings for evidence_id in finding.evidence_ids
        ))
        if not evidence_ids:
            evidence_ids = [item.evidence_id for item in report.evidence if item.supports_conclusion][:6]
        return DiagnosisRootCause(
            title=report.root_cause,
            description=" → ".join(report.root_cause_chain) or report.conclusion,
            root_resource=root_resource,
            confidence=report.confidence,
            evidence_ids=evidence_ids,
            recommendations=report.recommended_fix,
        )

    @staticmethod
    def _add_edge(
        edges: list[IncidentGraphEdge], keys: set[tuple[str, str, str]],
        source: str, target: str, relation: str,
        latency_ms: float | None = None, evidence_ids: list[str] | None = None,
    ) -> None:
        key = (source, target, relation)
        if key in keys:
            return
        keys.add(key)
        edges.append(IncidentGraphEdge(
            id=uuid4().hex, source=f"service:{source}", target=f"service:{target}",
            relation=relation, latency_ms=latency_ms, status="AFFECTED",
            evidence_ids=evidence_ids or [],
        ))

    @staticmethod
    def _target_summary(request: DiagnosisCreateRequest, target: str) -> str:
        if request.initial_target:
            return f"以 {request.initial_target.type.value} {request.initial_target.name} 为初始对象，允许沿依赖关系扩展调查范围。"
        if target != "unknown":
            return f"从问题中识别初始服务 {target}，允许继续发现关联资源。"
        return "问题未限定单个服务，将先执行全局只读巡检并从证据中发现调查对象。"

    @staticmethod
    def _tool_target(arguments: dict[str, Any], fallback: str | None) -> tuple[str | None, str | None]:
        for key, resource_type in (("pod", "POD"), ("pod_name", "POD"), ("service", "SERVICE"), ("service_name", "SERVICE")):
            if arguments.get(key):
                return resource_type, str(arguments[key])
        return ("SERVICE", fallback) if fallback else (None, None)

    @staticmethod
    def _evidence_resource_type(tool_name: str) -> str:
        if "pod" in tool_name or tool_name in {"get_restart_count", "get_pod_events", "get_container_image"}:
            return "POD"
        if tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}:
            return "DATABASE"
        return "SERVICE"

    @staticmethod
    def _evidence_resource_id(structured: dict[str, Any], fallback: str) -> str | None:
        pods = structured.get("pods", [])
        services = structured.get("services", [])
        return str(pods[0]) if pods else (str(services[0]) if services else (fallback or None))
