"""MySQL-owned immutable historical cases and restart-safe delivery bookkeeping."""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.core.database import ApplicationDatabase
from app.history.models import HistoryItem, HistoryQuery
from app.workflow.evidence_gate import build_report
from app.workflow.models import DiagnosisState


class HistoryRepository:
    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def capture_completed(self, limit: int = 20) -> int:
        """Reconcile committed diagnoses, including a crash before projection creation.

        Does not update Task/Checkpoint/Lease. Snapshot + pending status commit together.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with closing(self.database.connect()) as conn:
            rows = conn.execute("""
                SELECT s.id, s.user_id, s.project_id, s.question, s.summary, s.finished_at,
                       s.updated_at, s.initial_target_type, s.initial_target_id,
                       s.affected_services_json, s.checkpoint_json,
                       r.description, r.root_resource_type, r.evidence_ids_json
                FROM diagnosis_sessions s
                LEFT JOIN diagnosis_root_causes r ON r.diagnosis_id = s.id
                LEFT JOIN diagnosis_history_items h ON h.task_id = s.id
                WHERE s.status = 'COMPLETED' AND h.task_id IS NULL
                ORDER BY s.updated_at, s.id LIMIT ?
                """, (max(1, min(limit, 100)),)).fetchall()
            for row in rows:
                checkpoint = json.loads(row["checkpoint_json"] or "{}")
                # Durable runtime stores the workflow state as the checkpoint document.
                state = checkpoint.get("state", checkpoint)
                services = json.loads(row["affected_services_json"] or "[]")
                service = state.get("service") or (
                    row["initial_target_id"] if row["initial_target_type"] == "SERVICE" else None
                ) or (services[0] if services else "unknown")
                evidence = conn.execute("""
                    SELECT id, summary FROM diagnosis_evidence
                    WHERE diagnosis_id = ? AND supports_conclusion = TRUE
                    ORDER BY timestamp, id LIMIT 8
                    """, (row["id"],)).fetchall()
                resource = str(row["root_resource_type"] or "unknown").lower()
                category = {"database": "database", "pod": "runtime", "deployment": "deployment"}.get(resource, "unknown")
                # The checkpoint synthesis is a proposal, not the gated conclusion.
                # Older/incomplete checkpoints cannot establish a confirmed case.
                conclusion_status = "insufficient_evidence"
                try:
                    conclusion_status = build_report(DiagnosisState.model_validate(state)).status
                except ValidationError:
                    pass
                item = HistoryItem(
                    history_id=row["id"], task_id=row["id"], user_id=row["user_id"], project_id=row["project_id"],
                    service=service, timestamp=row["finished_at"] or row["updated_at"], category=category,
                    symptom=str(row["question"] or "")[:600], root_cause=str(row["description"] or "")[:1200],
                    summary=str(row["summary"] or "")[:1200], tags=[resource] if resource != "unknown" else [],
                    evidence_summary=[str(e["summary"])[:240] for e in evidence],
                    evidence_ids=[str(e["id"]) for e in evidence],
                    conclusion_status=conclusion_status,
                )
                result = conn.execute("""
                    INSERT INTO diagnosis_history_items
                        (history_id, task_id, user_id, project_id, document_json, next_retry_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE history_id = VALUES(history_id)
                    """, (item.history_id, item.task_id, item.user_id, item.project_id,
                          item.model_dump_json(), now, now))
                count += int(result.rowcount == 1)
            conn.commit()
        return count

    def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as conn:
            return [dict(row) for row in conn.execute("""
                SELECT history_id, document_json, retry_count FROM diagnosis_history_items
                WHERE index_status = 'pending' AND next_retry_at <= ?
                ORDER BY next_retry_at, history_id LIMIT ?
                """, (datetime.now(timezone.utc).isoformat(), max(1, min(limit, 100)))).fetchall()]

    def acknowledge(self, history_id: str) -> None:
        with closing(self.database.connect()) as conn:
            conn.execute("""UPDATE diagnosis_history_items SET index_status = 'indexed',
                indexed_at = ?, last_index_error = NULL WHERE history_id = ?""",
                (datetime.now(timezone.utc).isoformat(), history_id))
            conn.commit()

    def retry(self, history_id: str, attempts: int, error_type: str) -> None:
        next_time = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(attempts + 1, 11)))
        with closing(self.database.connect()) as conn:
            conn.execute("""UPDATE diagnosis_history_items SET retry_count = retry_count + 1,
                last_index_error = ?, next_retry_at = ? WHERE history_id = ? AND index_status = 'pending'""",
                (error_type[:100], next_time.isoformat(), history_id))
            conn.commit()

    def rebuild(self) -> int:
        """Requeue all source records; no Elasticsearch scan or deletion needed."""
        with closing(self.database.connect()) as conn:
            result = conn.execute("""UPDATE diagnosis_history_items SET index_status = 'pending',
                retry_count = 0, last_index_error = NULL, next_retry_at = ?""",
                (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
            return result.rowcount

    def hydrate(self, ids: list[str], user_id: str, project_id: str, query: HistoryQuery) -> list[HistoryItem]:
        """Treat ES as an untrusted index; enforce ownership/existence in MySQL too."""
        ids = list(dict.fromkeys(ids))[:10]
        if not ids:
            return []
        with closing(self.database.connect()) as conn:
            rows = conn.execute(f"""SELECT h.document_json FROM diagnosis_history_items h
                JOIN diagnosis_sessions s ON s.id = h.task_id
                WHERE h.user_id = ? AND h.project_id = ? AND s.user_id = ? AND s.project_id = ?
                  AND s.status = 'COMPLETED' AND h.history_id IN ({','.join('?' for _ in ids)})""",
                (user_id, project_id, user_id, project_id, *ids)).fetchall()
        found = {}
        for row in rows:
            item = HistoryItem.model_validate_json(row["document_json"])
            if (item.user_id != user_id or item.project_id != project_id or item.service != query.service
                or item.task_id == query.exclude_task_id or (query.category and item.category != query.category)
                or (query.start_time and item.timestamp < query.start_time)
                or (query.end_time and item.timestamp > query.end_time)
                or (query.tags and not set(query.tags).intersection(item.tags))):
                continue
            found[item.history_id] = item
        return [found[key] for key in ids if key in found][:query.limit]

    def pending_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute("""SELECT e.id, e.diagnosis_id, e.event_type, e.created_at,
                    s.user_id, s.project_id, s.initial_target_id
                FROM diagnosis_events e JOIN diagnosis_sessions s ON s.id = e.diagnosis_id
                WHERE NOT EXISTS (SELECT 1 FROM history_event_deliveries d WHERE d.event_id = e.id)
                ORDER BY e.id LIMIT ?""", (max(1, min(limit, 100)),)).fetchall()
        # Raw event payloads can contain secrets/tool output; publish metadata only.
        return [{"record_type": "event", "event_id": str(r["id"]), "task_id": r["diagnosis_id"],
                 "user_id": r["user_id"], "project_id": r["project_id"], "event_type": r["event_type"],
                 "timestamp": r["created_at"], "service_name": r["initial_target_id"] or "sre-agent",
                 "message": r["event_type"]} for r in rows]

    def acknowledge_event(self, event_id: int) -> None:
        with closing(self.database.connect()) as conn:
            # Auto-increment order is not commit order. Per-event acknowledgements
            # retain late commits with lower IDs instead of skipping them forever.
            conn.execute("""INSERT INTO history_event_deliveries(event_id, delivered_at)
                SELECT id, ? FROM diagnosis_events WHERE id = ?
                ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)""",
                (datetime.now(timezone.utc).isoformat(), event_id))
            conn.commit()
