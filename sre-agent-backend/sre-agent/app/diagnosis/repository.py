"""Diagnosis 模块的 MySQL Repository；所有读取都强制校验 user_id。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase
from app.diagnosis.models import (
    DiagnosisEvent, DiagnosisEvidence, DiagnosisRootCause, DiagnosisSession,
    DiagnosisStatus, DiagnosisTarget, IncidentGraph, IncidentGraphEdge,
    IncidentGraphNode, InvestigationStep, RootCauseResource,
)


class DiagnosisRepository:
    """提供 Diagnosis 聚合的持久化操作，不提供跨用户旁路。"""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def create(
        self,
        user_id: str,
        conversation_id: str,
        question: str,
        trigger_type: str,
        initial_target: DiagnosisTarget | None,
    ) -> DiagnosisSession:
        diagnosis_id = uuid4().hex
        now = self._now()
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_sessions(
                    id, user_id, conversation_id, question, trigger_type,
                    initial_target_type, initial_target_id, initial_target_namespace,
                    status, affected_services_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '[]', ?, ?)
                """,
                (
                    diagnosis_id, user_id, conversation_id, question, trigger_type,
                    initial_target.type.value if initial_target else None,
                    initial_target.name if initial_target else None,
                    initial_target.namespace if initial_target else None,
                    now, now,
                ),
            )
            connection.commit()
        return self.get(user_id, diagnosis_id)  # type: ignore[return-value]

    def get(self, user_id: str, diagnosis_id: str) -> DiagnosisSession | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM diagnosis_sessions WHERE id = ? AND user_id = ?",
                (diagnosis_id, user_id),
            ).fetchone()
        return self._session(row) if row else None

    def list_for_user(self, user_id: str, limit: int = 50) -> list[DiagnosisSession]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM diagnosis_sessions
                WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._session(row) for row in rows]

    def update_session(
        self,
        diagnosis_id: str,
        *,
        status: DiagnosisStatus | str | None = None,
        run_id: str | None = None,
        summary: str | None = None,
        affected_services: list[str] | None = None,
        error_message: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        assignments = ["updated_at = ?"]
        parameters: list[Any] = [self._now()]
        if status is not None:
            assignments.append("status = ?")
            parameters.append(status.value if isinstance(status, DiagnosisStatus) else status)
        if run_id is not None:
            assignments.append("run_id = ?")
            parameters.append(run_id)
        if summary is not None:
            assignments.append("summary = ?")
            parameters.append(summary)
        if affected_services is not None:
            assignments.append("affected_services_json = ?")
            parameters.append(self._json(list(dict.fromkeys(affected_services))))
        if error_message is not None:
            assignments.append("error_message = ?")
            parameters.append(error_message[:4000])
        if started:
            assignments.append("started_at = COALESCE(started_at, ?)")
            parameters.append(self._now())
        if finished:
            assignments.append("finished_at = ?")
            parameters.append(self._now())
        parameters.append(diagnosis_id)
        with closing(self.database.connect()) as connection:
            connection.execute(
                f"UPDATE diagnosis_sessions SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            connection.commit()

    def append_step(
        self,
        diagnosis_id: str,
        *,
        step_type: str,
        summary: str,
        status: str = "COMPLETED",
        target_type: str | None = None,
        target_id: str | None = None,
        tool_name: str | None = None,
        evidence_ids: list[str] | None = None,
        error_message: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> InvestigationStep:
        step_id = uuid4().hex
        started_value = started_at or self._now()
        finished_value = finished_at or (self._now() if status in {"COMPLETED", "FAILED"} else None)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_no FROM diagnosis_investigation_steps WHERE diagnosis_id = ?",
                (diagnosis_id,),
            ).fetchone()
            sequence_no = int(row["next_no"])
            connection.execute(
                """
                INSERT INTO diagnosis_investigation_steps(
                    id, diagnosis_id, sequence_no, step_type, target_type, target_id,
                    tool_name, status, started_at, finished_at, summary,
                    evidence_ids_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id, diagnosis_id, sequence_no, step_type, target_type, target_id,
                    tool_name, status, started_value, finished_value, summary[:8000],
                    self._json(evidence_ids or []), error_message[:4000] if error_message else None,
                ),
            )
            connection.commit()
        return InvestigationStep(
            id=step_id, diagnosis_id=diagnosis_id, sequence_no=sequence_no,
            step_type=step_type, target_type=target_type, target_id=target_id,
            tool_name=tool_name, status=status, started_at=started_value,
            finished_at=finished_value, summary=summary, evidence_ids=evidence_ids or [],
            error_message=error_message,
        )

    def list_steps(self, user_id: str, diagnosis_id: str) -> list[InvestigationStep]:
        rows = self._owned_rows(
            user_id, diagnosis_id,
            "SELECT s.* FROM diagnosis_investigation_steps s WHERE s.diagnosis_id = ? ORDER BY s.sequence_no",
        )
        return [InvestigationStep(
            id=str(row["id"]), diagnosis_id=str(row["diagnosis_id"]),
            sequence_no=int(row["sequence_no"]), step_type=str(row["step_type"]),
            target_type=str(row["target_type"]) if row["target_type"] else None,
            target_id=str(row["target_id"]) if row["target_id"] else None,
            tool_name=str(row["tool_name"]) if row["tool_name"] else None,
            status=str(row["status"]), started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            summary=str(row["summary"]), evidence_ids=self._loads(row["evidence_ids_json"], []),
            error_message=str(row["error_message"]) if row["error_message"] else None,
        ) for row in rows]

    def upsert_evidence(self, evidence: DiagnosisEvidence) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_evidence(
                    diagnosis_id, id, source_type, source_name, resource_type,
                    resource_id, title, summary, raw_data_json, metadata_json,
                    supports_conclusion, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE source_type = VALUES(source_type),
                    source_name = VALUES(source_name), resource_type = VALUES(resource_type),
                    resource_id = VALUES(resource_id), title = VALUES(title),
                    summary = VALUES(summary), raw_data_json = VALUES(raw_data_json),
                    metadata_json = VALUES(metadata_json),
                    supports_conclusion = VALUES(supports_conclusion), timestamp = VALUES(timestamp)
                """,
                (
                    evidence.diagnosis_id, evidence.id, evidence.source_type, evidence.source_name,
                    evidence.resource_type, evidence.resource_id, evidence.title[:255], evidence.summary,
                    self._json(evidence.raw_data), self._json(evidence.metadata),
                    evidence.supports_conclusion, evidence.timestamp,
                ),
            )
            connection.commit()

    def list_evidence(self, user_id: str, diagnosis_id: str) -> list[DiagnosisEvidence]:
        rows = self._owned_rows(
            user_id, diagnosis_id,
            "SELECT e.* FROM diagnosis_evidence e WHERE e.diagnosis_id = ? ORDER BY e.timestamp, e.id",
        )
        return [DiagnosisEvidence(
            id=str(row["id"]), diagnosis_id=str(row["diagnosis_id"]),
            source_type=str(row["source_type"]), source_name=str(row["source_name"]),
            resource_type=str(row["resource_type"]) if row["resource_type"] else None,
            resource_id=str(row["resource_id"]) if row["resource_id"] else None,
            title=str(row["title"]), summary=str(row["summary"]),
            raw_data=self._loads(row["raw_data_json"], {}),
            metadata=self._loads(row["metadata_json"], {}),
            supports_conclusion=bool(row["supports_conclusion"]), timestamp=str(row["timestamp"]),
        ) for row in rows]

    def replace_graph(self, diagnosis_id: str, graph: IncidentGraph) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("DELETE FROM diagnosis_graph_edges WHERE diagnosis_id = ?", (diagnosis_id,))
            connection.execute("DELETE FROM diagnosis_graph_nodes WHERE diagnosis_id = ?", (diagnosis_id,))
            for node in graph.nodes:
                connection.execute(
                    "INSERT INTO diagnosis_graph_nodes(diagnosis_id, node_id, node_type, name, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (diagnosis_id, node.id, node.type, node.name, node.status, self._json(node.metadata)),
                )
            for edge in graph.edges:
                connection.execute(
                    """
                    INSERT INTO diagnosis_graph_edges(
                        id, diagnosis_id, source_node_id, target_node_id, relation_type,
                        latency_ms, status, evidence_ids_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edge.id, diagnosis_id, edge.source, edge.target, edge.relation,
                        edge.latency_ms, edge.status, self._json(edge.evidence_ids),
                        self._json(edge.metadata),
                    ),
                )
            connection.commit()

    def get_graph(self, user_id: str, diagnosis_id: str) -> IncidentGraph:
        if not self.get(user_id, diagnosis_id):
            raise KeyError("diagnosis not found")
        with closing(self.database.connect()) as connection:
            nodes = connection.execute(
                "SELECT * FROM diagnosis_graph_nodes WHERE diagnosis_id = ? ORDER BY name", (diagnosis_id,)
            ).fetchall()
            edges = connection.execute(
                "SELECT * FROM diagnosis_graph_edges WHERE diagnosis_id = ? ORDER BY source_node_id, target_node_id",
                (diagnosis_id,),
            ).fetchall()
        return IncidentGraph(
            nodes=[IncidentGraphNode(
                id=str(row["node_id"]), type=str(row["node_type"]), name=str(row["name"]),
                status=str(row["status"]), metadata=self._loads(row["metadata_json"], {}),
            ) for row in nodes],
            edges=[IncidentGraphEdge(
                id=str(row["id"]), source=str(row["source_node_id"]), target=str(row["target_node_id"]),
                relation=str(row["relation_type"]), latency_ms=float(row["latency_ms"]) if row["latency_ms"] is not None else None,
                status=str(row["status"]), evidence_ids=self._loads(row["evidence_ids_json"], []),
                metadata=self._loads(row["metadata_json"], {}),
            ) for row in edges],
        )

    def upsert_root_cause(self, diagnosis_id: str, root: DiagnosisRootCause) -> None:
        now = self._now()
        resource = root.root_resource
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_root_causes(
                    diagnosis_id, title, description, root_resource_type,
                    root_resource_name, confidence, evidence_ids_json,
                    recommendations_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE title = VALUES(title), description = VALUES(description),
                    root_resource_type = VALUES(root_resource_type),
                    root_resource_name = VALUES(root_resource_name), confidence = VALUES(confidence),
                    evidence_ids_json = VALUES(evidence_ids_json),
                    recommendations_json = VALUES(recommendations_json), created_at = VALUES(created_at)
                """,
                (
                    diagnosis_id, root.title[:255], root.description,
                    resource.type if resource else None, resource.name if resource else None,
                    root.confidence, self._json(root.evidence_ids),
                    self._json(root.recommendations), now,
                ),
            )
            connection.commit()

    def get_root_cause(self, user_id: str, diagnosis_id: str) -> DiagnosisRootCause | None:
        rows = self._owned_rows(
            user_id, diagnosis_id,
            "SELECT r.* FROM diagnosis_root_causes r WHERE r.diagnosis_id = ?",
        )
        if not rows:
            return None
        row = rows[0]
        resource = None
        if row["root_resource_type"] and row["root_resource_name"]:
            resource = RootCauseResource(type=str(row["root_resource_type"]), name=str(row["root_resource_name"]))
        return DiagnosisRootCause(
            title=str(row["title"]), description=str(row["description"]),
            root_resource=resource, confidence=float(row["confidence"]),
            evidence_ids=self._loads(row["evidence_ids_json"], []),
            recommendations=self._loads(row["recommendations_json"], []),
        )

    def append_event(self, diagnosis_id: str, event_type: str, data: dict[str, Any]) -> int:
        with closing(self.database.connect()) as connection:
            connection.execute(
                "INSERT INTO diagnosis_events(diagnosis_id, event_type, data_json, created_at) VALUES (?, ?, ?, ?)",
                (diagnosis_id, event_type, self._json(data), self._now()),
            )
            row = connection.execute("SELECT LAST_INSERT_ID() AS id").fetchone()
            connection.commit()
        return int(row["id"])

    def list_events(self, user_id: str, diagnosis_id: str, after_id: int = 0) -> list[DiagnosisEvent]:
        rows = self._owned_rows(
            user_id, diagnosis_id,
            "SELECT e.* FROM diagnosis_events e WHERE e.diagnosis_id = ? AND e.id > ? ORDER BY e.id LIMIT 200",
            (after_id,),
        )
        return [DiagnosisEvent(
            id=int(row["id"]), diagnosis_id=str(row["diagnosis_id"]),
            type=str(row["event_type"]), data=self._loads(row["data_json"], {}),
            created_at=str(row["created_at"]),
        ) for row in rows]

    def _owned_rows(
        self,
        user_id: str,
        diagnosis_id: str,
        query: str,
        extra_parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        if not self.get(user_id, diagnosis_id):
            raise KeyError("diagnosis not found")
        with closing(self.database.connect()) as connection:
            return list(connection.execute(query, (diagnosis_id, *extra_parameters)).fetchall())

    @classmethod
    def _session(cls, row: dict[str, Any]) -> DiagnosisSession:
        target = None
        if row["initial_target_type"] and row["initial_target_id"]:
            target = DiagnosisTarget(
                type=str(row["initial_target_type"]), name=str(row["initial_target_id"]),
                namespace=str(row["initial_target_namespace"] or "sre-lab"),
            )
        return DiagnosisSession(
            id=str(row["id"]), conversation_id=str(row["conversation_id"]),
            run_id=str(row["run_id"]) if row["run_id"] else None,
            question=str(row["question"]), trigger_type=str(row["trigger_type"]),
            initial_target=target, status=str(row["status"]),
            summary=str(row["summary"]) if row["summary"] else None,
            affected_services=cls._loads(row["affected_services_json"], []),
            error_message=str(row["error_message"]) if row["error_message"] else None,
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if value is None or value == "":
            return default
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
