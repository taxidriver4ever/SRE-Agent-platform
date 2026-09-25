"""Diagnosis 模块的 MySQL Repository；所有读取都强制校验 user_id。"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase
from app.core.errors import TaskOwnershipLostError
from app.diagnosis.models import (
    DiagnosisEvent, DiagnosisEvidence, DiagnosisRootCause, DiagnosisSession,
    DiagnosisStatus, DiagnosisTarget, IncidentGraph, IncidentGraphEdge,
    IncidentGraphNode, InvestigationStep, RootCauseResource,
)


class DiagnosisOwnershipLost(TaskOwnershipLostError):
    """CAS 或 Lease 校验失败；当前 Executor 必须立即放弃写入。"""


class _ProjectionConnection:
    """Repository methods share an outer transaction without committing it early."""

    def __init__(self, connection):
        self.execute = connection.execute

    def commit(self):
        pass

    def close(self):
        pass


class DiagnosisRepository:
    """提供 Diagnosis 聚合的持久化操作，不提供跨用户旁路。"""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database
        self._projection = ContextVar("diagnosis_projection", default=None)

    def _projection_connect(self):
        return self._projection.get() or self.database.connect()

    def _lock_owner(self, connection, diagnosis_id: str, owner: str, version: int):
        """Always lock Task before Step/Evidence, including report projections."""
        row = connection.execute(
            "SELECT lease_owner, state_version, lease_expires_at, status, next_step_sequence FROM diagnosis_sessions WHERE id = ? FOR UPDATE",
            (diagnosis_id,),
        ).fetchone()
        if (row is None or row["lease_owner"] != owner or int(row["state_version"]) != version
                or row["status"] != "INVESTIGATING"
                or not row["lease_expires_at"] or str(row["lease_expires_at"]) <= self._now()):
            connection.rollback()
            raise DiagnosisOwnershipLost("task ownership check failed")
        return row

    @contextmanager
    def owned_projection(self, diagnosis_id: str, owner: str, version: int):
        """Fence synchronous report/start projections in the same SQL transaction.

        No await, framework callback or network operation may run inside this scope.
        Existing projection methods commit only when the outer transaction succeeds.
        """
        with closing(self.database.connect()) as connection:
            self._lock_owner(connection, diagnosis_id, owner, version)
            token = self._projection.set(_ProjectionConnection(connection))
            try:
                yield
                self._lock_owner(connection, diagnosis_id, owner, version)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._projection.reset(token)

    def create(
        self,
        user_id: str,
        conversation_id: str,
        question: str,
        trigger_type: str,
        initial_target: DiagnosisTarget | None,
        project_id: str = "sre-lab",
    ) -> DiagnosisSession:
        diagnosis_id = uuid4().hex
        now = self._now()
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_sessions(
                    id, user_id, conversation_id, project_id, question, trigger_type,
                    initial_target_type, initial_target_id, initial_target_namespace,
                    status, affected_services_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '[]', ?, ?)
                """,
                (
                    diagnosis_id, user_id, conversation_id, project_id, question, trigger_type,
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

    def get_unscoped(self, diagnosis_id: str) -> DiagnosisSession | None:
        """仅供进程内 Recovery/SelfCheck 使用，HTTP 读取仍必须走 user_id。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM diagnosis_sessions WHERE id = ?", (diagnosis_id,)
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
        with closing(self._projection_connect()) as connection:
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
        idempotency_key: str | None = None,
        arguments: dict[str, Any] | None = None,
        result: Any = None,
        attempt_no: int = 1,
        evidence_id: str | None = None,
    ) -> InvestigationStep:
        step_id = uuid4().hex
        started_value = started_at or self._now()
        finished_value = finished_at or (self._now() if status in {"COMPLETED", "FAILED"} else None)
        with closing(self._projection_connect()) as connection:
            session = connection.execute(
                "SELECT next_step_sequence FROM diagnosis_sessions WHERE id = ? FOR UPDATE",
                (diagnosis_id,),
            ).fetchone()
            if session is None:
                raise KeyError("diagnosis not found")
            sequence_no = int(session["next_step_sequence"])
            connection.execute(
                "UPDATE diagnosis_sessions SET next_step_sequence = ?, updated_at = ? WHERE id = ?",
                (sequence_no + 1, self._now(), diagnosis_id),
            )
            connection.execute(
                """
                INSERT INTO diagnosis_investigation_steps(
                    id, diagnosis_id, sequence_no, idempotency_key, step_type, target_type, target_id,
                    tool_name, arguments_json, result_json, attempt_no, evidence_id,
                    status, started_at, finished_at, summary,
                    evidence_ids_json, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id, diagnosis_id, sequence_no, idempotency_key, step_type, target_type, target_id,
                    tool_name, self._json(arguments or {}), self._json(result) if result is not None else None,
                    attempt_no, evidence_id, status, started_value, finished_value, summary[:8000],
                    self._json(evidence_ids or []), error_message[:4000] if error_message else None, self._now(),
                ),
            )
            connection.commit()
        return InvestigationStep(
            id=step_id, diagnosis_id=diagnosis_id, sequence_no=sequence_no,
            step_type=step_type, target_type=target_type, target_id=target_id,
            tool_name=tool_name, status=status, started_at=started_value,
            finished_at=finished_value, summary=summary, evidence_ids=evidence_ids or [],
            error_message=error_message,
            idempotency_key=idempotency_key, arguments=arguments or {}, result=result,
            attempt_no=attempt_no, evidence_id=evidence_id, updated_at=self._now(),
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
            idempotency_key=str(row["idempotency_key"]) if row.get("idempotency_key") else None,
            arguments=self._loads(row.get("arguments_json"), {}),
            result=self._loads(row.get("result_json"), None),
            attempt_no=int(row.get("attempt_no") or 1),
            evidence_id=str(row["evidence_id"]) if row.get("evidence_id") else None,
            updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
        ) for row in rows]

    def get_step_by_key(self, diagnosis_id: str, idempotency_key: str) -> InvestigationStep | None:
        with closing(self._projection_connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM diagnosis_investigation_steps
                WHERE diagnosis_id = ? AND idempotency_key = ?
                """,
                (diagnosis_id, idempotency_key),
            ).fetchone()
        return self._step(row) if row else None

    def get_step_by_evidence_id(self, diagnosis_id: str, evidence_id: str) -> InvestigationStep | None:
        with closing(self._projection_connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM diagnosis_investigation_steps
                WHERE diagnosis_id = ? AND evidence_id = ? LIMIT 1
                """,
                (diagnosis_id, evidence_id),
            ).fetchone()
        return self._step(row) if row else None

    def list_running_steps_unscoped(self, diagnosis_id: str) -> list[InvestigationStep]:
        """供持有 Lease 的 Runtime 找回 crash window 内尚未提交的 Tool。"""
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM diagnosis_investigation_steps
                WHERE diagnosis_id = ? AND step_type = 'TOOL' AND status = 'RUNNING'
                ORDER BY sequence_no
                """,
                (diagnosis_id,),
            ).fetchall()
        return [self._step(row) for row in rows]

    def upsert_evidence(self, evidence: DiagnosisEvidence) -> None:
        with closing(self._projection_connect()) as connection:
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

    def get_evidence_unscoped(
        self, diagnosis_id: str, evidence_id: str,
    ) -> DiagnosisEvidence | None:
        """供已取得 Session Lease 的 Runtime 恢复单条 Evidence。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM diagnosis_evidence WHERE diagnosis_id = ? AND id = ?",
                (diagnosis_id, evidence_id),
            ).fetchone()
        if row is None:
            return None
        return DiagnosisEvidence(
            id=str(row["id"]), diagnosis_id=str(row["diagnosis_id"]),
            source_type=str(row["source_type"]), source_name=str(row["source_name"]),
            resource_type=str(row["resource_type"]) if row["resource_type"] else None,
            resource_id=str(row["resource_id"]) if row["resource_id"] else None,
            title=str(row["title"]), summary=str(row["summary"]),
            raw_data=self._loads(row["raw_data_json"], {}),
            metadata=self._loads(row["metadata_json"], {}),
            supports_conclusion=bool(row["supports_conclusion"]),
            timestamp=str(row["timestamp"]),
        )

    def replace_graph(self, diagnosis_id: str, graph: IncidentGraph) -> None:
        with closing(self._projection_connect()) as connection:
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
        with closing(self._projection_connect()) as connection:
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

    def append_event(
        self, diagnosis_id: str, event_type: str, data: dict[str, Any], *, event_key: str | None = None,
    ) -> int:
        with closing(self._projection_connect()) as connection:
            result = connection.execute(
                """
                INSERT INTO diagnosis_events(diagnosis_id, event_type, event_key, data_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """,
                (diagnosis_id, event_type, event_key, self._json(data), self._now()),
            )
            row = connection.execute("SELECT LAST_INSERT_ID() AS id").fetchone()
            connection.commit()
        return int(row["id"] or 0) if row else (1 if result.rowcount else 0)

    def claim(
        self,
        diagnosis_id: str,
        lease_owner: str,
        *,
        lease_ttl_seconds: float,
        max_attempts: int,
        recovery_reason: str,
    ) -> DiagnosisSession | None:
        """原子取得执行权；只有受影响行数为 1 才能启动 asyncio Executor。"""
        now = self._now()
        expires = self._future(lease_ttl_seconds)
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET status = 'INVESTIGATING', lease_owner = ?, lease_expires_at = ?,
                    heartbeat_at = ?, attempt_no = attempt_no + 1,
                    state_version = state_version + 1,
                    started_at = COALESCE(started_at, ?), interrupted_at = NULL,
                    recovery_reason = ?, updated_at = ?
                WHERE id = ?
                  AND status IN ('PENDING', 'INVESTIGATING')
                  AND attempt_no < ?
                  AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (
                    lease_owner, expires, now, now, recovery_reason[:255], now,
                    diagnosis_id, max_attempts, now,
                ),
            )
            connection.commit()
        if result.rowcount != 1:
            return None
        return self.get_unscoped(diagnosis_id)

    def heartbeat(
        self, diagnosis_id: str, lease_owner: str, expected_version: int, lease_ttl_seconds: float,
    ) -> int:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET heartbeat_at = ?, lease_expires_at = ?, state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state_version = ?
                  AND status = 'INVESTIGATING' AND lease_expires_at > ?
                """,
                (now, self._future(lease_ttl_seconds), now, diagnosis_id, lease_owner, expected_version, now),
            )
            connection.commit()
        if result.rowcount != 1:
            raise DiagnosisOwnershipLost("heartbeat CAS failed")
        return expected_version + 1

    def save_checkpoint(
        self,
        diagnosis_id: str,
        lease_owner: str,
        expected_version: int,
        checkpoint: dict[str, Any],
        *,
        current_phase: str,
        phase_status: str,
        lease_ttl_seconds: float,
        event_type: str = "checkpoint.saved",
        event_key: str | None = None,
    ) -> int:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET run_id = COALESCE(run_id, ?), current_phase = ?, phase_status = ?,
                    checkpoint_json = ?, checkpoint_seq = checkpoint_seq + 1,
                    heartbeat_at = ?, lease_expires_at = ?, state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state_version = ?
                  AND status = 'INVESTIGATING' AND lease_expires_at > ?
                """,
                (
                    checkpoint.get("run_id"), current_phase, phase_status,
                    self._json(checkpoint), now, self._future(lease_ttl_seconds), now,
                    diagnosis_id, lease_owner, expected_version, now,
                ),
            )
            if result.rowcount != 1:
                connection.rollback()
                raise DiagnosisOwnershipLost("checkpoint CAS failed")
            self._insert_event(
                connection, diagnosis_id, event_type,
                {
                    "phase": current_phase, "phase_status": phase_status,
                    "checkpoint_seq": "incremented",
                },
                event_key,
            )
            connection.commit()
        return expected_version + 1

    def begin_tool_step(
        self,
        diagnosis_id: str,
        lease_owner: str,
        expected_version: int,
        *,
        idempotency_key: str,
        evidence_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        parent_evidence_ids: list[str],
        target_type: str | None,
        target_id: str | None,
        started_at: str,
    ) -> tuple[InvestigationStep, bool, int]:
        """返回 ``(step, completed_cache_hit, state_version)``。"""
        now = self._now()
        with closing(self.database.connect()) as connection:
            session = self._lock_owner(connection, diagnosis_id, lease_owner, expected_version)
            existing = connection.execute(
                """
                SELECT * FROM diagnosis_investigation_steps
                WHERE diagnosis_id = ? AND idempotency_key = ? FOR UPDATE
                """,
                (diagnosis_id, idempotency_key),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "COMPLETED":
                return self._step(existing), True, expected_version

            if existing is not None:
                connection.execute(
                    """
                    UPDATE diagnosis_investigation_steps
                    SET status = 'RUNNING', attempt_no = attempt_no + 1,
                        arguments_json = ?, parent_evidence_ids_json = ?,
                        error_message = NULL, finished_at = NULL, updated_at = ?
                    WHERE diagnosis_id = ? AND idempotency_key = ?
                    """,
                    (
                        self._json(arguments), self._json(parent_evidence_ids), now,
                        diagnosis_id, idempotency_key,
                    ),
                )
                step_id = str(existing["id"])
                sequence_no = int(existing["sequence_no"])
                attempt_no = int(existing["attempt_no"] or 1) + 1
            else:
                step_id = uuid4().hex
                sequence_no = int(session["next_step_sequence"])
                attempt_no = 1
                connection.execute(
                    """
                    INSERT INTO diagnosis_investigation_steps(
                        id, diagnosis_id, sequence_no, idempotency_key, step_type,
                        target_type, target_id, tool_name, arguments_json, result_json,
                        parent_evidence_ids_json,
                        attempt_no, evidence_id, status, started_at, finished_at,
                        summary, evidence_ids_json, error_message, updated_at
                    ) VALUES (?, ?, ?, ?, 'TOOL', ?, ?, ?, ?, NULL, ?, ?, ?, 'RUNNING', ?, NULL, ?, '[]', NULL, ?)
                    """,
                    (
                        step_id, diagnosis_id, sequence_no, idempotency_key,
                        target_type, target_id, tool_name, self._json(arguments),
                        self._json(parent_evidence_ids), attempt_no, evidence_id,
                        started_at, "工具执行中", now,
                    ),
                )
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET next_step_sequence = CASE WHEN next_step_sequence <= ? THEN ? ELSE next_step_sequence END,
                    state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state_version = ?
                """,
                (sequence_no, sequence_no + 1, now, diagnosis_id, lease_owner, expected_version),
            )
            if result.rowcount != 1:
                connection.rollback()
                raise DiagnosisOwnershipLost("tool sequence allocation CAS failed")
            self._insert_event(
                connection, diagnosis_id, "step.started",
                {
                    "id": step_id, "diagnosis_id": diagnosis_id, "sequence_no": sequence_no,
                    "tool_name": tool_name, "target_type": target_type, "target_id": target_id,
                    "started_at": started_at, "attempt_no": attempt_no,
                },
                f"step.started:{idempotency_key}:{attempt_no}",
            )
            connection.commit()
        step = InvestigationStep(
            id=step_id, diagnosis_id=diagnosis_id, sequence_no=sequence_no,
            idempotency_key=idempotency_key, step_type="TOOL", target_type=target_type,
            target_id=target_id, tool_name=tool_name, arguments=arguments,
            status="RUNNING", started_at=started_at, summary="工具执行中",
            attempt_no=attempt_no, evidence_id=evidence_id, updated_at=now,
        )
        return step, False, expected_version + 1

    def complete_tool_step(
        self,
        diagnosis_id: str,
        lease_owner: str,
        expected_version: int,
        *,
        idempotency_key: str,
        result_value: Any,
        step_status: str,
        summary: str,
        error_message: str | None,
        evidence: DiagnosisEvidence | None,
        checkpoint: dict[str, Any],
        current_phase: str,
        lease_ttl_seconds: float,
    ) -> int:
        """Step、Evidence、Checkpoint 和 completed/failed Event 使用同一事务。"""
        now = self._now()
        with closing(self.database.connect()) as connection:
            self._lock_owner(connection, diagnosis_id, lease_owner, expected_version)
            row = connection.execute(
                """
                SELECT * FROM diagnosis_investigation_steps
                WHERE diagnosis_id = ? AND idempotency_key = ? FOR UPDATE
                """,
                (diagnosis_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise KeyError("logical tool step not found")
            if evidence is not None:
                self._upsert_evidence_connection(connection, evidence)
            evidence_ids = [evidence.id] if evidence is not None else []
            connection.execute(
                """
                UPDATE diagnosis_investigation_steps
                SET status = ?, result_json = ?, finished_at = ?, summary = ?,
                    evidence_ids_json = ?, error_message = ?, updated_at = ?
                WHERE diagnosis_id = ? AND idempotency_key = ?
                """,
                (
                    step_status, self._json(result_value) if result_value is not None else None,
                    now, summary[:8000], self._json(evidence_ids),
                    error_message[:4000] if error_message else None, now,
                    diagnosis_id, idempotency_key,
                ),
            )
            updated = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET run_id = COALESCE(run_id, ?), checkpoint_json = ?,
                    checkpoint_seq = checkpoint_seq + 1, heartbeat_at = ?, lease_expires_at = ?,
                    state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state_version = ?
                  AND status = 'INVESTIGATING' AND lease_expires_at > ?
                """,
                (
                    checkpoint.get("run_id"), self._json(checkpoint), now,
                    self._future(lease_ttl_seconds), now,
                    diagnosis_id, lease_owner, expected_version, now,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise DiagnosisOwnershipLost("tool completion CAS failed")
            event_name = "step.completed" if step_status == "COMPLETED" else "step.failed"
            payload = {
                "id": str(row["id"]), "diagnosis_id": diagnosis_id,
                "sequence_no": int(row["sequence_no"]), "tool_name": str(row["tool_name"]),
                "status": step_status, "summary": summary, "evidence_ids": evidence_ids,
                "error_message": error_message, "attempt_no": int(row["attempt_no"] or 1),
            }
            self._insert_event(
                connection, diagnosis_id, event_name, payload,
                (
                    f"{event_name}:{idempotency_key}"
                    if step_status == "COMPLETED"
                    else f"{event_name}:{idempotency_key}:{int(row['attempt_no'] or 1)}"
                ),
            )
            self._insert_event(
                connection, diagnosis_id, "checkpoint.saved",
                {"phase": current_phase, "operation": idempotency_key},
                f"checkpoint.tool:{idempotency_key}",
            )
            connection.commit()
        return expected_version + 1

    def interrupt(self, diagnosis_id: str, lease_owner: str, reason: str) -> None:
        """进程生命周期中断不是业务 CANCELLED；释放 Lease 以便下次启动恢复。"""
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    interrupted_at = ?, recovery_reason = ?, state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'INVESTIGATING'
                """,
                (now, reason[:255], now, diagnosis_id, lease_owner),
            )
            if result.rowcount == 1:
                self._insert_event(
                    connection, diagnosis_id, "diagnosis.interrupted",
                    {"diagnosis_id": diagnosis_id, "reason": reason},
                    f"diagnosis.interrupted:{lease_owner}",
                )
            connection.commit()

    def list_recoverable(
        self, now: str | None = None, limit: int = 20,
    ) -> list[DiagnosisSession]:
        """按创建时间返回一批可恢复任务；每次调用只读取固定数量且不使用 OFFSET。"""
        cutoff = now or self._now()
        bounded_limit = max(1, min(200, int(limit)))
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM diagnosis_sessions
                WHERE status = 'PENDING'
                   OR (status = 'INVESTIGATING' AND (
                        lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?
                   ))
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (cutoff, bounded_limit),
            ).fetchall()
        return [self._session(row) for row in rows]

    def mark_max_attempts_exceeded(self, diagnosis_id: str, max_attempts: int) -> bool:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET status = 'FAILED', error_message = 'maximum recovery attempts exceeded',
                    finished_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND status IN ('PENDING', 'INVESTIGATING') AND attempt_no >= ?
                """,
                (now, now, diagnosis_id, max_attempts),
            )
            if result.rowcount == 1:
                self._insert_event(
                    connection, diagnosis_id, "diagnosis.failed",
                    {"diagnosis_id": diagnosis_id, "message": "maximum recovery attempts exceeded"},
                    "diagnosis.failed",
                )
            connection.commit()
        return result.rowcount == 1

    def complete_owned_session(
        self,
        diagnosis_id: str,
        lease_owner: str,
        expected_version: int,
        *,
        run_id: str,
        summary: str,
        affected_services: list[str],
    ) -> int:
        """以 CAS 完成 Session，并在同一事务写唯一 completed Event。"""
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET status = 'COMPLETED', run_id = ?, summary = ?, affected_services_json = ?,
                    current_phase = 'END', phase_status = 'COMPLETED', finished_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state_version = ?
                  AND status = 'INVESTIGATING' AND lease_expires_at > ?
                """,
                (
                    run_id, summary, self._json(list(dict.fromkeys(affected_services))),
                    now, now, diagnosis_id, lease_owner, expected_version, now,
                ),
            )
            if result.rowcount != 1:
                current = connection.execute(
                    "SELECT status, run_id, state_version FROM diagnosis_sessions WHERE id = ?",
                    (diagnosis_id,),
                ).fetchone()
                if (
                    current is not None
                    and str(current["status"]) == "COMPLETED"
                    and str(current["run_id"] or "") == run_id
                ):
                    connection.rollback()
                    return int(current["state_version"])
                connection.rollback()
                raise DiagnosisOwnershipLost("final session CAS failed")
            self._insert_event(
                connection, diagnosis_id, "diagnosis.completed",
                {
                    "diagnosis_id": diagnosis_id, "status": "COMPLETED",
                    "summary": summary, "affected_services": affected_services,
                },
                "diagnosis.completed",
            )
            connection.commit()
        return expected_version + 1

    def fail_owned_session(
        self, diagnosis_id: str, lease_owner: str, error_message: str,
    ) -> bool:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """
                UPDATE diagnosis_sessions
                SET status = 'FAILED', error_message = ?, finished_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'INVESTIGATING' AND lease_expires_at > ?
                """,
                (error_message[:4000], now, now, diagnosis_id, lease_owner, now),
            )
            if result.rowcount == 1:
                self._insert_event(
                    connection, diagnosis_id, "diagnosis.failed",
                    {"diagnosis_id": diagnosis_id, "status": "FAILED", "message": error_message[:4000]},
                    "diagnosis.failed",
                )
            connection.commit()
        return result.rowcount == 1

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

    def self_check_snapshot(self, user_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """只读返回 Runtime 一致性检查所需的最小关系数据。"""
        definitions = {
            "sessions": ("SELECT session.* FROM diagnosis_sessions session", "session.created_at"),
            "steps": ("SELECT item.* FROM diagnosis_investigation_steps item", "item.diagnosis_id, item.sequence_no"),
            "evidence": ("SELECT item.diagnosis_id, item.id, item.metadata_json FROM diagnosis_evidence item", "item.diagnosis_id, item.id"),
            "roots": ("SELECT item.diagnosis_id, item.evidence_ids_json FROM diagnosis_root_causes item", "item.diagnosis_id"),
            "nodes": ("SELECT item.diagnosis_id, item.node_id FROM diagnosis_graph_nodes item", "item.diagnosis_id, item.node_id"),
            "edges": ("SELECT item.diagnosis_id, item.source_node_id, item.target_node_id FROM diagnosis_graph_edges item", "item.diagnosis_id, item.id"),
            "events": ("SELECT item.diagnosis_id, item.event_type, item.event_key FROM diagnosis_events item", "item.diagnosis_id, item.id"),
        }
        with closing(self.database.connect()) as connection:
            output: dict[str, list[dict[str, Any]]] = {}
            for name, (select, order_by) in definitions.items():
                if user_id is None:
                    query = f"{select} ORDER BY {order_by}"
                    parameters: tuple[Any, ...] = ()
                elif name == "sessions":
                    query = f"{select} WHERE session.user_id = ? ORDER BY {order_by}"
                    parameters = (user_id,)
                else:
                    query = (
                        f"{select} JOIN diagnosis_sessions session ON session.id = item.diagnosis_id "
                        f"WHERE session.user_id = ? ORDER BY {order_by}"
                    )
                    parameters = (user_id,)
                output[name] = list(connection.execute(query, parameters).fetchall())
            return output

    def durable_schema_metadata(self) -> dict[str, set[str]]:
        """返回 durable execution 所需表的列与索引，供 Level 1 Self-Check。"""
        with closing(self.database.connect()) as connection:
            columns = connection.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ('diagnosis_sessions', 'diagnosis_investigation_steps', 'diagnosis_events')
                """
            ).fetchall()
            indexes = connection.execute(
                """
                SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ('diagnosis_sessions', 'diagnosis_investigation_steps', 'diagnosis_events')
                """
            ).fetchall()
        return {
            "columns": {f"{row['TABLE_NAME']}.{row['COLUMN_NAME']}" for row in columns},
            "indexes": {f"{row['TABLE_NAME']}.{row['INDEX_NAME']}" for row in indexes},
        }

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
            id=str(row["id"]), user_id=str(row["user_id"]),
            conversation_id=str(row["conversation_id"]), project_id=str(row.get("project_id") or "sre-lab"),
            run_id=str(row["run_id"]) if row["run_id"] else None,
            question=str(row["question"]), trigger_type=str(row["trigger_type"]),
            initial_target=target, status=str(row["status"]),
            current_phase=str(row["current_phase"]) if row.get("current_phase") else None,
            phase_status=str(row.get("phase_status") or "PENDING"),
            checkpoint_json=str(row["checkpoint_json"]) if row.get("checkpoint_json") else None,
            checkpoint_seq=int(row.get("checkpoint_seq") or 0),
            attempt_no=int(row.get("attempt_no") or 0),
            heartbeat_at=str(row["heartbeat_at"]) if row.get("heartbeat_at") else None,
            lease_owner=str(row["lease_owner"]) if row.get("lease_owner") else None,
            lease_expires_at=str(row["lease_expires_at"]) if row.get("lease_expires_at") else None,
            state_version=int(row.get("state_version") or 0),
            next_step_sequence=int(row.get("next_step_sequence") or 1),
            interrupted_at=str(row["interrupted_at"]) if row.get("interrupted_at") else None,
            recovery_reason=str(row["recovery_reason"]) if row.get("recovery_reason") else None,
            summary=str(row["summary"]) if row["summary"] else None,
            affected_services=cls._loads(row["affected_services_json"], []),
            error_message=str(row["error_message"]) if row["error_message"] else None,
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @classmethod
    def _step(cls, row: dict[str, Any]) -> InvestigationStep:
        return InvestigationStep(
            id=str(row["id"]), diagnosis_id=str(row["diagnosis_id"]),
            sequence_no=int(row["sequence_no"]), step_type=str(row["step_type"]),
            target_type=str(row["target_type"]) if row.get("target_type") else None,
            target_id=str(row["target_id"]) if row.get("target_id") else None,
            tool_name=str(row["tool_name"]) if row.get("tool_name") else None,
            status=str(row["status"]), started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]) if row.get("finished_at") else None,
            summary=str(row["summary"]), evidence_ids=cls._loads(row.get("evidence_ids_json"), []),
            error_message=str(row["error_message"]) if row.get("error_message") else None,
            idempotency_key=str(row["idempotency_key"]) if row.get("idempotency_key") else None,
            arguments=cls._loads(row.get("arguments_json"), {}),
            parent_evidence_ids=cls._loads(row.get("parent_evidence_ids_json"), []),
            result=cls._loads(row.get("result_json"), None),
            attempt_no=int(row.get("attempt_no") or 1),
            evidence_id=str(row["evidence_id"]) if row.get("evidence_id") else None,
            updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
        )

    def _upsert_evidence_connection(self, connection: Any, evidence: DiagnosisEvidence) -> None:
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

    def _insert_event(
        self, connection: Any, diagnosis_id: str, event_type: str,
        data: dict[str, Any], event_key: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO diagnosis_events(diagnosis_id, event_type, event_key, data_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE id = id
            """,
            (diagnosis_id, event_type, event_key, self._json(data), self._now()),
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

    @staticmethod
    def _future(seconds: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
