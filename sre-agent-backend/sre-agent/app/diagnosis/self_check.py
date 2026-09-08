"""Read-only Diagnosis Runtime consistency and recoverability checks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.diagnosis.models import (
    DiagnosisSelfCheckReport, SelfCheckIssue, SelfCheckStatus,
)
from app.diagnosis.repository import DiagnosisRepository
from app.workflow.models import DiagnosisState


class DiagnosisSelfCheckService:
    """只读取 Application MySQL 与当前 Executor 状态，不调用任何 SRE Tool 或 LLM。"""

    REQUIRED_COLUMNS = {
        "diagnosis_sessions.current_phase", "diagnosis_sessions.phase_status",
        "diagnosis_sessions.checkpoint_json", "diagnosis_sessions.checkpoint_seq",
        "diagnosis_sessions.attempt_no", "diagnosis_sessions.heartbeat_at",
        "diagnosis_sessions.lease_owner", "diagnosis_sessions.lease_expires_at",
        "diagnosis_sessions.state_version", "diagnosis_sessions.next_step_sequence",
        "diagnosis_investigation_steps.idempotency_key",
        "diagnosis_investigation_steps.arguments_json",
        "diagnosis_investigation_steps.parent_evidence_ids_json",
        "diagnosis_investigation_steps.result_json",
        "diagnosis_investigation_steps.attempt_no",
        "diagnosis_investigation_steps.evidence_id",
        "diagnosis_events.event_key",
    }
    REQUIRED_INDEXES = {
        "diagnosis_investigation_steps.uk_diagnosis_step_idempotency",
        "diagnosis_events.uk_diagnosis_event_key",
        "diagnosis_sessions.idx_diagnoses_recovery",
    }
    TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

    def __init__(
        self,
        repository: DiagnosisRepository,
        execution_manager: Any,
        *,
        max_attempts: int = 3,
        lease_ttl_seconds: float = 60,
        heartbeat_interval_seconds: float = 10,
    ) -> None:
        self.repository = repository
        self.execution_manager = execution_manager
        self.max_attempts = max_attempts
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def check(self, level: int = 3, *, user_id: str | None = None) -> DiagnosisSelfCheckReport:
        now = datetime.now(timezone.utc)
        issues: list[SelfCheckIssue] = []
        try:
            metadata = self.repository.durable_schema_metadata()
            missing_columns = sorted(self.REQUIRED_COLUMNS - metadata["columns"])
            missing_indexes = sorted(self.REQUIRED_INDEXES - metadata["indexes"])
            if missing_columns or missing_indexes:
                self._issue(
                    issues, "DURABLE_SCHEMA_INCOMPLETE", "CRITICAL", None,
                    "Diagnosis durable execution 所需字段或索引缺失", False,
                    {"missing_columns": missing_columns, "missing_indexes": missing_indexes},
                )
            if not str(getattr(self.execution_manager, "executor_id", "")).strip():
                self._issue(
                    issues, "EXECUTOR_ID_MISSING", "CRITICAL", None,
                    "当前进程没有 executor_id", False,
                )
            if not bool(getattr(self.execution_manager, "heartbeat_subsystem_ready", True)):
                self._issue(
                    issues, "HEARTBEAT_SUBSYSTEM_UNAVAILABLE", "CRITICAL", None,
                    "Heartbeat subsystem 当前不可用", False,
                )
            snapshot = self.repository.self_check_snapshot(user_id=user_id) if level >= 2 else {
                name: [] for name in ("sessions", "steps", "evidence", "roots", "nodes", "edges", "events")
            }
        except Exception as exc:
            self._issue(
                issues, "APPLICATION_MYSQL_UNAVAILABLE", "CRITICAL", None,
                f"Application MySQL 自检失败: {exc}", False,
            )
            return self._report(now, [], issues)

        sessions = snapshot["sessions"]
        steps = snapshot["steps"]
        evidence_rows = snapshot["evidence"]
        roots = snapshot["roots"]
        nodes = snapshot["nodes"]
        edges = snapshot["edges"]
        events = snapshot["events"]
        by_id = {str(row["id"]): row for row in sessions}

        for session in sessions:
            self._check_session(session, now, issues)
        active = [row for row in sessions if str(row["status"]) in {"PENDING", "INVESTIGATING"}]
        if active and not bool(getattr(self.execution_manager, "heartbeat_running", False)):
            # PENDING 尚未 claim 时不要求心跳；只有当前 executor 持有 lease 才检查子系统。
            own_active = any(
                str(row.get("lease_owner") or "") == str(getattr(self.execution_manager, "executor_id", ""))
                for row in active
            )
            if own_active:
                self._issue(
                    issues, "HEARTBEAT_SUBSYSTEM_STOPPED", "ERROR", None,
                    "当前 Executor 持有 Diagnosis Lease，但 heartbeat subsystem 未运行", True,
                )

        if level >= 3:
            self._check_relations(by_id, steps, evidence_rows, roots, nodes, edges, events, issues)
        return self._report(now, sessions, issues)

    def basic_check(self) -> DiagnosisSelfCheckReport:
        return self.check(level=1)

    def _check_session(
        self, session: dict[str, Any], now: datetime, issues: list[SelfCheckIssue],
    ) -> None:
        diagnosis_id = str(session["id"])
        status = str(session["status"])
        owner = str(session.get("lease_owner") or "")
        expiry = self._parse_time(session.get("lease_expires_at"))
        heartbeat = self._parse_time(session.get("heartbeat_at"))
        attempt_no = int(session.get("attempt_no") or 0)

        if status == "INVESTIGATING":
            if not owner:
                self._issue(
                    issues, "MISSING_ACTIVE_LEASE", "WARNING", diagnosis_id,
                    "INVESTIGATING Diagnosis 没有 Lease，可由 Recovery claim", True,
                )
            elif expiry is None or expiry <= now:
                self._issue(
                    issues, "STALE_LEASE", "WARNING", diagnosis_id,
                    "Diagnosis Lease 已过期并具备恢复条件", True,
                    {"lease_expires_at": session.get("lease_expires_at")},
                )
            elif expiry - now <= timedelta(seconds=self.heartbeat_interval_seconds * 1.5):
                self._issue(
                    issues, "LEASE_EXPIRING_SOON", "WARNING", diagnosis_id,
                    "当前 Executor 的 Lease 即将过期", True,
                    {"lease_expires_at": session.get("lease_expires_at")},
                )
            if heartbeat and now - heartbeat > timedelta(seconds=self.lease_ttl_seconds):
                self._issue(
                    issues, "STALE_HEARTBEAT", "WARNING", diagnosis_id,
                    "Heartbeat 超过 Lease TTL 未更新", True,
                    {"heartbeat_at": session.get("heartbeat_at")},
                )
            if (not owner or expiry is None or expiry <= now) and attempt_no >= self.max_attempts:
                self._issue(
                    issues, "RECOVERY_ATTEMPTS_EXHAUSTED", "ERROR", diagnosis_id,
                    "Diagnosis 已失去有效 Lease，且恢复次数已经耗尽", False,
                    {"attempt_no": attempt_no, "max_attempts": self.max_attempts},
                )
            checkpoint = session.get("checkpoint_json")
            if not checkpoint:
                self._issue(
                    issues, "MISSING_CHECKPOINT", "ERROR", diagnosis_id,
                    "INVESTIGATING Diagnosis 缺少 DiagnosisState Checkpoint", False,
                )
            else:
                try:
                    state = DiagnosisState.model_validate_json(str(checkpoint))
                except Exception as exc:
                    self._issue(
                        issues, "INVALID_CHECKPOINT", "ERROR", diagnosis_id,
                        f"Checkpoint 无法反序列化为 DiagnosisState: {exc}", False,
                    )
                else:
                    if str(state.run_id) != str(session.get("run_id") or ""):
                        self._issue(
                            issues, "CHECKPOINT_RUN_ID_MISMATCH", "ERROR", diagnosis_id,
                            "Checkpoint run_id 与 Session run_id 不一致", False,
                            {"session_run_id": session.get("run_id"), "checkpoint_run_id": state.run_id},
                        )
                    current_phase = str(session.get("current_phase") or "")
                    if current_phase and current_phase not in {phase.value for phase in state.phases}:
                        self._issue(
                            issues, "CHECKPOINT_PHASE_MISMATCH", "ERROR", diagnosis_id,
                            "current_phase 不在 Checkpoint phases 中", False,
                            {"current_phase": current_phase},
                        )

        if status in self.TERMINAL and owner:
            self._issue(
                issues, "TERMINAL_SESSION_HOLDS_LEASE", "ERROR", diagnosis_id,
                f"{status} Session 不应继续持有 Lease", False,
            )
        if status == "PENDING" and owner:
            self._issue(
                issues, "PENDING_SESSION_HOLDS_LEASE", "ERROR", diagnosis_id,
                "PENDING Session 不应持有 Lease", False,
            )
        if status in self.TERMINAL and not session.get("finished_at"):
            self._issue(
                issues, "TERMINAL_SESSION_WITHOUT_FINISH_TIME", "ERROR", diagnosis_id,
                f"{status} Session 缺少 finished_at", False,
            )
        if status == "COMPLETED" and str(session.get("current_phase") or "") != "END":
            self._issue(
                issues, "COMPLETED_PHASE_NOT_END", "ERROR", diagnosis_id,
                "COMPLETED Session 的 current_phase 必须为 END", False,
            )
        if int(session.get("checkpoint_seq") or 0) < 0 or int(session.get("state_version") or 0) < 0:
            self._issue(
                issues, "INVALID_STATE_COUNTER", "ERROR", diagnosis_id,
                "checkpoint_seq/state_version 不能为负数", False,
            )
        if int(session.get("checkpoint_seq") or 0) > int(session.get("state_version") or 0):
            self._issue(
                issues, "STATE_COUNTER_REGRESSION", "ERROR", diagnosis_id,
                "checkpoint_seq 不应大于 state_version", False,
            )
        if attempt_no > self.max_attempts:
            self._issue(
                issues, "MAX_ATTEMPTS_EXCEEDED", "ERROR", diagnosis_id,
                "attempt_no 超过配置的最大恢复次数", False,
                {"attempt_no": attempt_no, "max_attempts": self.max_attempts},
            )

    def _check_relations(
        self,
        sessions: dict[str, dict[str, Any]],
        steps: list[dict[str, Any]],
        evidence_rows: list[dict[str, Any]],
        roots: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        events: list[dict[str, Any]],
        issues: list[SelfCheckIssue],
    ) -> None:
        evidence = {(str(row["diagnosis_id"]), str(row["id"])) for row in evidence_rows}
        step_keys: set[tuple[str, str]] = set()
        report_steps: set[str] = set()
        now = datetime.now(timezone.utc)
        for step in steps:
            diagnosis_id = str(step["diagnosis_id"])
            session = sessions.get(diagnosis_id)
            if str(step.get("step_type") or "") == "REPORT" and str(step["status"]) == "COMPLETED":
                report_steps.add(diagnosis_id)
            if session and str(session["status"]) in self.TERMINAL and str(step["status"]) == "RUNNING":
                self._issue(
                    issues, "ORPHAN_RUNNING_STEP", "ERROR", diagnosis_id,
                    "Terminal Diagnosis 仍存在 RUNNING InvestigationStep", False,
                    {"step_id": step["id"]},
                )
            if session and str(session["status"]) == "INVESTIGATING" and str(step["status"]) == "RUNNING":
                expiry = self._parse_time(session.get("lease_expires_at"))
                if not session.get("lease_owner") or expiry is None or expiry <= now:
                    self._issue(
                        issues, "INTERRUPTED_RUNNING_STEP", "WARNING", diagnosis_id,
                        "RUNNING Step 所属 Session 已无有效 Lease；恢复时应复用 logical step 并增加 attempt", True,
                        {"step_id": step["id"], "attempt_no": int(step.get("attempt_no") or 1)},
                    )
            key = str(step.get("idempotency_key") or "")
            if key:
                identity = (diagnosis_id, key)
                if identity in step_keys:
                    self._issue(
                        issues, "DUPLICATE_LOGICAL_STEP", "ERROR", diagnosis_id,
                        "同一 Diagnosis 存在重复 logical Tool Step", False,
                        {"idempotency_key": key},
                    )
                step_keys.add(identity)
            if (
                str(step.get("step_type") or "") == "TOOL"
                and str(step["status"]) == "COMPLETED"
                and step.get("result_json") in (None, "")
            ):
                self._issue(
                    issues, "COMPLETED_STEP_WITHOUT_RESULT", "ERROR", diagnosis_id,
                    "COMPLETED Tool Step 缺少可恢复的 result_json", False,
                    {"step_id": step["id"]},
                )
            declared_evidence_id = str(step.get("evidence_id") or "")
            evidence_ids = [str(item) for item in self._loads(step.get("evidence_ids_json"), [])]
            if declared_evidence_id and evidence_ids and declared_evidence_id not in evidence_ids:
                self._issue(
                    issues, "STEP_EVIDENCE_ID_MISMATCH", "ERROR", diagnosis_id,
                    "Step 的稳定 evidence_id 与 evidence_ids_json 不一致", False,
                    {"step_id": step["id"], "evidence_id": declared_evidence_id},
                )
            for evidence_id in evidence_ids:
                if (diagnosis_id, str(evidence_id)) not in evidence:
                    self._issue(
                        issues, "MISSING_STEP_EVIDENCE", "ERROR", diagnosis_id,
                        "InvestigationStep 引用了不存在的 Evidence", False,
                        {"step_id": step["id"], "evidence_id": evidence_id},
                    )

        for row in evidence_rows:
            diagnosis_id = str(row["diagnosis_id"])
            metadata = self._loads(row.get("metadata_json"), {})
            logical_key = str(metadata.get("logical_step_key") or "")
            if logical_key and (diagnosis_id, logical_key) not in step_keys:
                self._issue(
                    issues, "EVIDENCE_WITHOUT_LOGICAL_STEP", "ERROR", diagnosis_id,
                    "Evidence 指向不存在的 logical Tool Step", False,
                    {"evidence_id": row["id"], "logical_step_key": logical_key},
                )

        roots_by_diagnosis = {str(row["diagnosis_id"]): row for row in roots}
        event_types = {(str(row["diagnosis_id"]), str(row["event_type"])) for row in events}
        for diagnosis_id, session in sessions.items():
            status = str(session["status"])
            if status == "COMPLETED" and diagnosis_id not in roots_by_diagnosis:
                self._issue(
                    issues, "COMPLETED_WITHOUT_ROOT_CAUSE", "ERROR", diagnosis_id,
                    "COMPLETED Session 缺少 Root Cause", False,
                )
            if status == "COMPLETED" and diagnosis_id not in report_steps:
                self._issue(
                    issues, "COMPLETED_WITHOUT_REPORT_STEP", "ERROR", diagnosis_id,
                    "COMPLETED Session 缺少已完成的 REPORT Step", False,
                )
            expected_event = {"COMPLETED": "diagnosis.completed", "FAILED": "diagnosis.failed"}.get(status)
            if expected_event and (diagnosis_id, expected_event) not in event_types:
                self._issue(
                    issues, "MISSING_TERMINAL_EVENT", "WARNING", diagnosis_id,
                    f"{status} Session 缺少 {expected_event} Event", True,
                )
            if int(session.get("attempt_no") or 0) > 1 and (diagnosis_id, "diagnosis.recovered") not in event_types:
                self._issue(
                    issues, "MISSING_RECOVERY_EVENT", "WARNING", diagnosis_id,
                    "多次执行的 Session 缺少 diagnosis.recovered Event", True,
                )

        for diagnosis_id, root in roots_by_diagnosis.items():
            for evidence_id in self._loads(root.get("evidence_ids_json"), []):
                if (diagnosis_id, str(evidence_id)) not in evidence:
                    self._issue(
                        issues, "MISSING_ROOT_CAUSE_EVIDENCE", "ERROR", diagnosis_id,
                        "Root Cause 引用了不存在的 Evidence", False,
                        {"evidence_id": evidence_id},
                    )

        node_keys = {(str(row["diagnosis_id"]), str(row["node_id"])) for row in nodes}
        for edge in edges:
            diagnosis_id = str(edge["diagnosis_id"])
            missing = [
                node_id for node_id in (str(edge["source_node_id"]), str(edge["target_node_id"]))
                if (diagnosis_id, node_id) not in node_keys
            ]
            if missing:
                self._issue(
                    issues, "DANGLING_GRAPH_EDGE", "ERROR", diagnosis_id,
                    "Graph Edge 引用了不存在的 Node", False,
                    {"missing_node_ids": missing},
                )

    def _report(
        self, now: datetime, sessions: list[dict[str, Any]], issues: list[SelfCheckIssue],
    ) -> DiagnosisSelfCheckReport:
        severities = {issue.severity for issue in issues}
        status = (
            SelfCheckStatus.UNHEALTHY if severities & {"ERROR", "CRITICAL"}
            else SelfCheckStatus.DEGRADED if issues else SelfCheckStatus.HEALTHY
        )
        active = [row for row in sessions if str(row["status"]) in {"PENDING", "INVESTIGATING"}]
        expired = [
            row for row in active
            if row.get("lease_owner") and (
                self._parse_time(row.get("lease_expires_at")) or now
            ) <= now
        ]
        return DiagnosisSelfCheckReport(
            status=status, checked_at=now.isoformat(), total_diagnoses=len(sessions),
            active_diagnoses=len(active),
            stale_diagnoses=sum(issue.code in {"STALE_LEASE", "MISSING_ACTIVE_LEASE"} for issue in issues),
            active_leases=sum(bool(row.get("lease_owner")) for row in active) - len(expired),
            expired_leases=len(expired),
            invalid_checkpoints=sum(issue.code in {"INVALID_CHECKPOINT", "MISSING_CHECKPOINT"} for issue in issues),
            orphan_steps=sum(issue.code == "ORPHAN_RUNNING_STEP" for issue in issues),
            consistency_errors=sum(issue.severity in {"ERROR", "CRITICAL"} for issue in issues),
            issues=issues,
        )

    @staticmethod
    def _issue(
        issues: list[SelfCheckIssue], code: str, severity: str,
        diagnosis_id: str | None, message: str, recoverable: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        issues.append(SelfCheckIssue(
            code=code, severity=severity, diagnosis_id=diagnosis_id,
            message=message, recoverable=recoverable, metadata=metadata or {},
        ))

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        try:
            return json.loads(str(value)) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default
