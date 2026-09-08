"""Diagnosis Runtime Self-Check 的分层、只读一致性与 API 契约测试。"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_user
from app.diagnosis.models import SelfCheckStatus
from app.diagnosis.self_check import DiagnosisSelfCheckService
from app.diagnosis.self_check_router import get_self_check_service, router
from app.workflow.models import DiagnosisState, WorkflowPhase


class _Manager:
    executor_id = "self-check-executor"
    heartbeat_running = False


class _Repository:
    def __init__(self, snapshot=None, *, complete_schema=True):
        self.snapshot = snapshot or _empty_snapshot()
        self.complete_schema = complete_schema
        self.requested_user_id = None

    def durable_schema_metadata(self):
        if self.complete_schema:
            return {
                "columns": set(DiagnosisSelfCheckService.REQUIRED_COLUMNS),
                "indexes": set(DiagnosisSelfCheckService.REQUIRED_INDEXES),
            }
        return {"columns": set(), "indexes": set()}

    def self_check_snapshot(self, user_id=None):
        self.requested_user_id = user_id
        return self.snapshot


def _empty_snapshot():
    return {name: [] for name in ("sessions", "steps", "evidence", "roots", "nodes", "edges", "events")}


def _time(delta_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _checkpoint(run_id="run-1", phases=None):
    state = DiagnosisState(
        query="timeout", conversation_id="conversation-1", run_id=run_id,
        phases=phases or [WorkflowPhase.START, WorkflowPhase.INVESTIGATE],
    )
    return state.model_dump_json()


def _session(**overrides):
    value = {
        "id": "diagnosis-1", "status": "INVESTIGATING", "run_id": "run-1",
        "current_phase": "INVESTIGATE", "phase_status": "RUNNING",
        "checkpoint_json": _checkpoint(), "checkpoint_seq": 2, "state_version": 4,
        "attempt_no": 1, "heartbeat_at": _time(), "lease_owner": "another-executor",
        "lease_expires_at": _time(60), "finished_at": None, "created_at": _time(-10),
    }
    value.update(overrides)
    return value


def _codes(report):
    return {issue.code for issue in report.issues}


def test_healthy_runtime_has_no_issues() -> None:
    service = DiagnosisSelfCheckService(_Repository(), _Manager())
    report = service.check(level=3)
    assert report.status is SelfCheckStatus.HEALTHY
    assert report.issues == []


def test_expired_lease_is_degraded_and_recoverable() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(lease_expires_at=_time(-30), heartbeat_at=_time(-30))]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=2)
    assert report.status is SelfCheckStatus.DEGRADED
    issue = next(item for item in report.issues if item.code == "STALE_LEASE")
    assert issue.recoverable is True
    assert report.stale_diagnoses == 1


def test_invalid_checkpoint_is_unhealthy() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(checkpoint_json="{broken-json")]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=2)
    assert report.status is SelfCheckStatus.UNHEALTHY
    assert "INVALID_CHECKPOINT" in _codes(report)
    assert report.invalid_checkpoints == 1


def test_checkpoint_run_id_and_phase_mismatch_are_detected() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(
        run_id="run-a", current_phase="VERIFY",
        checkpoint_json=_checkpoint("run-b", [WorkflowPhase.START, WorkflowPhase.INVESTIGATE]),
    )]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=2)
    assert {"CHECKPOINT_RUN_ID_MISMATCH", "CHECKPOINT_PHASE_MISMATCH"} <= _codes(report)


def test_terminal_lease_and_orphan_running_step_are_unhealthy() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(
        status="COMPLETED", current_phase="END", phase_status="COMPLETED",
        lease_owner="dead-executor", lease_expires_at=_time(60), finished_at=_time(),
    )]
    snapshot["steps"] = [{
        "id": "step-1", "diagnosis_id": "diagnosis-1", "step_type": "TOOL",
        "status": "RUNNING", "idempotency_key": "key-1", "evidence_id": "ev-1",
        "evidence_ids_json": "[]", "result_json": None, "attempt_no": 1,
    }]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=3)
    assert report.status is SelfCheckStatus.UNHEALTHY
    assert {"TERMINAL_SESSION_HOLDS_LEASE", "ORPHAN_RUNNING_STEP"} <= _codes(report)
    assert report.orphan_steps == 1


def test_missing_step_and_root_evidence_are_detected() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(
        status="COMPLETED", current_phase="END", phase_status="COMPLETED",
        lease_owner=None, lease_expires_at=None, finished_at=_time(),
    )]
    snapshot["steps"] = [
        {
            "id": "tool", "diagnosis_id": "diagnosis-1", "step_type": "TOOL",
            "status": "COMPLETED", "idempotency_key": "key", "evidence_id": "missing",
            "evidence_ids_json": json.dumps(["missing"]), "result_json": "{}", "attempt_no": 1,
        },
        {
            "id": "report", "diagnosis_id": "diagnosis-1", "step_type": "REPORT",
            "status": "COMPLETED", "idempotency_key": "final-report", "evidence_id": None,
            "evidence_ids_json": "[]", "result_json": "{}", "attempt_no": 1,
        },
    ]
    snapshot["roots"] = [{"diagnosis_id": "diagnosis-1", "evidence_ids_json": '["missing"]'}]
    snapshot["events"] = [{
        "diagnosis_id": "diagnosis-1", "event_type": "diagnosis.completed",
        "event_key": "diagnosis.completed",
    }]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=3)
    assert {"MISSING_STEP_EVIDENCE", "MISSING_ROOT_CAUSE_EVIDENCE"} <= _codes(report)


def test_dangling_graph_edge_is_detected() -> None:
    snapshot = _empty_snapshot()
    snapshot["nodes"] = [{"diagnosis_id": "diagnosis-1", "node_id": "service:a"}]
    snapshot["edges"] = [{
        "diagnosis_id": "diagnosis-1", "source_node_id": "service:a",
        "target_node_id": "service:missing",
    }]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=3)
    assert "DANGLING_GRAPH_EDGE" in _codes(report)


def test_interrupted_running_step_is_recoverable() -> None:
    snapshot = _empty_snapshot()
    snapshot["sessions"] = [_session(lease_expires_at=_time(-1))]
    snapshot["steps"] = [{
        "id": "step-1", "diagnosis_id": "diagnosis-1", "step_type": "TOOL",
        "status": "RUNNING", "idempotency_key": "key", "evidence_id": "ev",
        "evidence_ids_json": "[]", "result_json": None, "attempt_no": 1,
    }]
    report = DiagnosisSelfCheckService(_Repository(snapshot), _Manager()).check(level=3)
    issue = next(item for item in report.issues if item.code == "INTERRUPTED_RUNNING_STEP")
    assert issue.recoverable is True


def test_missing_schema_is_unhealthy_at_level_one() -> None:
    report = DiagnosisSelfCheckService(
        _Repository(complete_schema=False), _Manager(),
    ).check(level=1)
    assert report.status is SelfCheckStatus.UNHEALTHY
    assert "DURABLE_SCHEMA_INCOMPLETE" in _codes(report)


def test_authenticated_self_check_scopes_snapshot_to_current_user() -> None:
    repository = _Repository()
    service = DiagnosisSelfCheckService(repository, _Manager())
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_user] = lambda: {"id": "user-1", "username": "tester"}
    application.dependency_overrides[get_self_check_service] = lambda: service
    client = TestClient(application)

    assert client.get("/api/system/self-check").status_code == 200
    assert repository.requested_user_id == "user-1"
    assert client.get("/api/system/self-check?level=4").status_code == 422


def test_health_endpoint_does_not_call_full_self_check() -> None:
    class _MustNotRun:
        def check(self, *_args, **_kwargs):
            raise AssertionError("liveness must not execute full self-check")

    application = FastAPI()

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    application.state.diagnosis_self_check = _MustNotRun()
    response = TestClient(application).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
