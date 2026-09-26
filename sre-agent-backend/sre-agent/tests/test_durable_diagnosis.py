"""Diagnosis durable state、Lease、Checkpoint 与 logical idempotency 回归测试。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.diagnosis.execution import DiagnosisExecutionManager, DurableWorkflowRuntime
from app.diagnosis.models import DiagnosisEvidence
from app.diagnosis.orchestrator import DiagnosisOrchestrator
from app.diagnosis.repository import DiagnosisRepository
from app.diagnosis.schemas import DiagnosisCreateRequest
from app.diagnosis.self_check import DiagnosisSelfCheckService
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.models import DiagnosisReport, DiagnosisState, Evidence, ToolCallRecord, WorkflowPhase
from tests.mysql_support import mysql_test_database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session():
    database = mysql_test_database(reset=False)
    user_id = uuid4().hex
    conversation_id = uuid4().hex
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO users(id, username, password_hash, created_at) VALUES (?, ?, 'test', ?)",
            (user_id, f"durable-{uuid4().hex[:8]}", _now()),
        )
        connection.execute(
            "INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES (?, ?, 'test', ?, ?)",
            (conversation_id, user_id, _now(), _now()),
        )
        connection.commit()
    repository = DiagnosisRepository(database)
    session = repository.create(
        user_id, conversation_id, "order-service timeout", "QUESTION", None,
    )
    return database, repository, session


def _state(session, run_id: str = "run-durable-1") -> DiagnosisState:
    return DiagnosisState(
        query=session.question, conversation_id=session.conversation_id,
        user_id=session.user_id, run_id=run_id, service="order-service",
    )


def _expired() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()


def _pending_sessions(count: int):
    """在同一用户与会话下创建一组顺序稳定的 PENDING Diagnosis。"""
    database, repository, first = _session()
    sessions = [first]
    for index in range(1, count):
        sessions.append(repository.create(
            str(first.user_id), first.conversation_id,
            f"queued diagnosis {index}", "QUESTION", None,
        ))
    with database.connect() as connection:
        for index, session in enumerate(sessions):
            connection.execute(
                "UPDATE diagnosis_sessions SET created_at = ? WHERE id = ?",
                (f"2026-01-01T00:00:{index:02d}+00:00", session.id),
            )
        connection.commit()
    return database, repository, sessions


def test_phase_cursor_resumes_after_completed_triage() -> None:
    _, repository, session = _session()
    claimed = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="test",
    )
    assert claimed is not None
    state = _state(session)
    state.phases = [WorkflowPhase.START, WorkflowPhase.TRIAGE]
    version = repository.save_checkpoint(
        session.id, "executor-a", claimed.state_version, state.model_dump(mode="json"),
        current_phase="TRIAGE", phase_status="COMPLETED", lease_ttl_seconds=60,
    )
    resumed = repository.get_unscoped(session.id)
    assert resumed is not None and resumed.state_version == version
    runtime = DurableWorkflowRuntime(repository, resumed, "executor-a", 60)

    assert runtime.should_skip_phase(WorkflowPhase.START)
    assert runtime.should_skip_phase(WorkflowPhase.TRIAGE)
    assert not runtime.should_skip_phase(WorkflowPhase.BASELINE_OBSERVATION)
    assert DiagnosisState.model_validate_json(resumed.checkpoint_json).run_id == "run-durable-1"


@pytest.mark.asyncio
async def test_initial_checkpoint_persists_run_id_before_other_side_effects() -> None:
    database, repository, session = _session()
    claimed = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="initial checkpoint",
    )
    assert claimed is not None
    runtime = DurableWorkflowRuntime(repository, claimed, "executor-a", 60)
    state = _state(session, "early-run-id")
    await runtime.initialize(state)
    await runtime.initialize(state)

    persisted = repository.get_unscoped(session.id)
    assert persisted.run_id == "early-run-id"
    assert DiagnosisState.model_validate_json(persisted.checkpoint_json).run_id == "early-run-id"
    with database.connect() as connection:
        initialized = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_events WHERE diagnosis_id = ? AND event_key = 'checkpoint.initialized'",
            (session.id,),
        ).fetchone()
    assert int(initialized["total"]) == 1


@pytest.mark.asyncio
async def test_completed_tool_is_loaded_without_second_external_call() -> None:
    _, repository, session = _session()
    claimed_session = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="test",
    )
    assert claimed_session is not None
    runtime = DurableWorkflowRuntime(repository, claimed_session, "executor-a", 60)
    state = _state(session)
    state.phases = [WorkflowPhase.INVESTIGATE]
    arguments = {"service": "order-service", "time_range_minutes": 30}
    claim = await runtime.begin_tool(
        state, WorkflowPhase.INVESTIGATE, "query_logs", arguments, "logs", [],
    )
    evidence = Evidence(
        source="ELASTICSEARCH", source_type="ELASTICSEARCH", tool_name="query_logs", title="logs",
        detail="timeout", summary="timeout", structured_data={"services": ["order-service"]},
        timestamp=datetime.now(timezone.utc), evidence_id=claim.evidence_id,
    )
    record = ToolCallRecord(
        tool_name="query_logs", arguments=arguments, result_summary="timeout",
        timestamp=datetime.now(timezone.utc), duration_ms=1, evidence_id=claim.evidence_id,
    )
    state.evidence.append(evidence)
    state.timeline.append(record)
    await runtime.complete_tool(
        state, WorkflowPhase.INVESTIGATE, claim, record, evidence, {"lines": ["timeout"]},
    )

    class _Tools:
        calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("completed logical tool must not execute again")

    workflow = object.__new__(DiagnosisWorkflow)
    workflow.max_steps = 12
    workflow.tools = _Tools()
    workflow.conversation_service = None
    workflow.kubernetes_namespace = "sre-lab"
    resumed = repository.get_unscoped(session.id)
    resumed_runtime = DurableWorkflowRuntime(repository, resumed, "executor-a", 60)
    empty_rehydrated = _state(session)
    empty_rehydrated.phases = [WorkflowPhase.INVESTIGATE]

    result = await workflow._call(
        empty_rehydrated, "query_logs", arguments, "logs", None,
        runtime=resumed_runtime, phase=WorkflowPhase.INVESTIGATE,
    )

    assert result == {"lines": ["timeout"]}
    assert workflow.tools.calls == 0
    assert len(empty_rehydrated.timeline) == 1
    assert len(empty_rehydrated.evidence) == 1


@pytest.mark.asyncio
async def test_running_tool_reuses_one_logical_step_and_increments_attempt() -> None:
    database, repository, session = _session()
    first = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="first",
    )
    assert first is not None
    state = _state(session)
    runtime = DurableWorkflowRuntime(repository, first, "executor-a", 60)
    first_claim = await runtime.begin_tool(
        state, WorkflowPhase.INVESTIGATE, "query_metrics", {"query": "up"}, "up", [],
    )
    repository.interrupt(session.id, "executor-a", "simulated crash")
    second = repository.claim(
        session.id, "executor-b", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="recovery",
    )
    assert second is not None
    resumed_runtime = DurableWorkflowRuntime(repository, second, "executor-b", 60)
    second_claim = await resumed_runtime.begin_tool(
        state, WorkflowPhase.INVESTIGATE, "query_metrics", {"query": "up"}, "up", [],
    )

    assert second_claim.idempotency_key == first_claim.idempotency_key
    assert second_claim.evidence_id == first_claim.evidence_id
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT attempt_no FROM diagnosis_investigation_steps WHERE diagnosis_id = ?",
            (session.id,),
        ).fetchall()
    assert [int(row["attempt_no"]) for row in rows] == [2]


def test_lease_claim_is_exclusive_and_fresh_lease_is_not_recoverable() -> None:
    _, repository, session = _session()
    first = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="race-a",
    )
    second = repository.claim(
        session.id, "executor-b", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="race-b",
    )
    assert first is not None
    assert second is None
    assert all(item.id != session.id for item in repository.list_recoverable())


def test_stale_lease_is_recoverable_but_terminal_states_are_not() -> None:
    database, repository, session = _session()
    claimed = repository.claim(
        session.id, "old-executor", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="test",
    )
    assert claimed is not None
    with database.connect() as connection:
        connection.execute(
            "UPDATE diagnosis_sessions SET lease_expires_at = ?, heartbeat_at = ? WHERE id = ?",
            (_expired(), _expired(), session.id),
        )
        connection.commit()
    assert [item.id for item in repository.list_recoverable()] == [session.id]

    for status in ("COMPLETED", "FAILED", "CANCELLED"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE diagnosis_sessions SET status = ?, lease_owner = NULL WHERE id = ?",
                (status, session.id),
            )
            connection.commit()
        assert all(item.id != session.id for item in repository.list_recoverable())


def test_recoverable_batch_is_limited_and_oldest_pending_sessions_are_first() -> None:
    """超过批次时只返回前 N 个，并按 created_at 从旧到新稳定排序。"""
    _, repository, sessions = _pending_sessions(30)

    recovered = repository.list_recoverable(limit=20)

    assert len(recovered) == 20
    assert [item.id for item in recovered] == [item.id for item in sessions[:20]]


def test_recoverable_batch_includes_only_pending_or_stale_investigating() -> None:
    """终态和有效 Lease 不恢复，PENDING 与过期/缺失 Lease 可以恢复。"""
    database, repository, sessions = _pending_sessions(7)
    pending, expired, missing_lease, valid, completed, failed, cancelled = sessions
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE diagnosis_sessions SET status = 'INVESTIGATING', lease_owner = 'old', lease_expires_at = ? WHERE id = ?",
            (_expired(), expired.id),
        )
        connection.execute(
            "UPDATE diagnosis_sessions SET status = 'INVESTIGATING', lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (missing_lease.id,),
        )
        connection.execute(
            "UPDATE diagnosis_sessions SET status = 'INVESTIGATING', lease_owner = 'active', lease_expires_at = ? WHERE id = ?",
            (future, valid.id),
        )
        for session, status in (
            (completed, "COMPLETED"), (failed, "FAILED"), (cancelled, "CANCELLED"),
        ):
            connection.execute(
                "UPDATE diagnosis_sessions SET status = ? WHERE id = ?",
                (status, session.id),
            )
        connection.commit()

    recovered_ids = {item.id for item in repository.list_recoverable(limit=20)}

    assert recovered_ids == {pending.id, expired.id, missing_lease.id}


@pytest.mark.asyncio
async def test_recovery_marks_attempt_limit_failed_without_starting_executor() -> None:
    database, repository, session = _session()
    with database.connect() as connection:
        connection.execute(
            "UPDATE diagnosis_sessions SET status = 'INVESTIGATING', attempt_no = 3, lease_owner = NULL WHERE id = ?",
            (session.id,),
        )
        connection.commit()
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), repository, _Sandbox(), max_attempts=3,
    )
    assert await manager.recover_stale_diagnoses() == 0
    failed = repository.get_unscoped(session.id)
    assert failed.status.value == "FAILED"
    assert failed.error_message == "maximum recovery attempts exceeded"
    assert manager.tasks == set()


def test_event_and_evidence_upserts_are_logically_idempotent() -> None:
    database, repository, session = _session()
    event_id = repository.append_event(
        session.id, "phase.completed", {"phase": "TRIAGE"},
        event_key="phase.completed:TRIAGE",
    )
    assert repository.append_event(
        session.id, "phase.completed", {"phase": "TRIAGE"},
        event_key="phase.completed:TRIAGE",
    ) == event_id
    evidence = DiagnosisEvidence(
        id="stable-evidence", diagnosis_id=session.id, source_type="ELASTICSEARCH",
        source_name="query_logs", title="logs", summary="first", timestamp=_now(),
    )
    repository.upsert_evidence(evidence)
    repository.upsert_evidence(evidence.model_copy(update={"summary": "updated"}))
    with database.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_events WHERE diagnosis_id = ? AND event_key = ?",
            (session.id, "phase.completed:TRIAGE"),
        ).fetchone()
        evidence_count = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_evidence WHERE diagnosis_id = ? AND id = ?",
            (session.id, "stable-evidence"),
        ).fetchone()
    assert int(event_count["total"]) == 1
    assert int(evidence_count["total"]) == 1


class _WaitingOrchestrator:
    async def run(self, *_args, **_kwargs):
        await asyncio.Event().wait()


class _Sandbox:
    @asynccontextmanager
    async def task_workspace(self, _task_id):
        yield Path.cwd()


class _EmptyRepository:
    def list_recoverable(self, *, limit=20):
        del limit
        return []


def test_recovery_batch_size_config_defaults_and_bounds(monkeypatch) -> None:
    monkeypatch.delenv("DIAGNOSIS_RECOVERY_BATCH_SIZE", raising=False)
    assert get_settings().diagnosis_recovery_batch_size == 20

    monkeypatch.setenv("DIAGNOSIS_RECOVERY_BATCH_SIZE", "0")
    assert get_settings().diagnosis_recovery_batch_size == 1

    monkeypatch.setenv("DIAGNOSIS_RECOVERY_BATCH_SIZE", "9999")
    assert get_settings().diagnosis_recovery_batch_size == 200


@pytest.mark.asyncio
async def test_each_recovery_tick_submits_only_one_batch_then_next_tick_continues() -> None:
    """单轮最多提交 batch_size；任务 claim 后，下一轮再处理剩余任务。"""
    database, repository, sessions = _pending_sessions(30)
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), repository, _Sandbox(),
        lease_ttl_seconds=60, heartbeat_interval_seconds=30,
        recovery_batch_size=20, max_attempts=3,
    )

    assert await manager.recover_stale_diagnoses() == 20
    assert len(manager.tasks) == 20
    with database.connect() as connection:
        before_claim = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_sessions WHERE status = 'PENDING'"
        ).fetchone()
    assert int(before_claim["total"]) == 30

    for _ in range(100):
        await asyncio.sleep(0.01)
        with database.connect() as connection:
            claimed = connection.execute(
                "SELECT COUNT(*) AS total FROM diagnosis_sessions WHERE lease_owner = ?",
                (manager.executor_id,),
            ).fetchone()
        if int(claimed["total"]) == 20:
            break
    assert int(claimed["total"]) == 20

    assert await manager.recover_stale_diagnoses() == 10
    assert len(manager.tasks) == 30
    for _ in range(100):
        await asyncio.sleep(0.01)
        with database.connect() as connection:
            claimed = connection.execute(
                "SELECT COUNT(*) AS total FROM diagnosis_sessions WHERE lease_owner = ?",
                (manager.executor_id,),
            ).fetchone()
        if int(claimed["total"]) == 30:
            break
    assert int(claimed["total"]) == 30
    assert {
        item.id for item in repository.list_recoverable(limit=20)
    }.isdisjoint({item.id for item in sessions})
    await manager.shutdown()


@pytest.mark.asyncio
async def test_periodic_recovery_claims_after_fresh_crashed_lease_expires() -> None:
    database, repository, session = _session()
    first = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="first attempt",
    )
    assert first is not None
    lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE diagnosis_sessions SET lease_expires_at = ? WHERE id = ?",
            (lease_expires_at, session.id),
        )
        connection.commit()

    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), repository, _Sandbox(),
        lease_ttl_seconds=60, heartbeat_interval_seconds=30,
        recovery_scan_interval_seconds=0.02, max_attempts=3,
    )
    assert await manager.recover_stale_diagnoses() == 0
    assert repository.get_unscoped(session.id).attempt_no == 1

    recovery_task = manager.start_recovery_loop()
    for _ in range(300):
        await asyncio.sleep(0.01)
        recovered = repository.get_unscoped(session.id)
        if recovered.attempt_no == 2 and recovered.lease_owner == manager.executor_id:
            break

    recovered = repository.get_unscoped(session.id)
    assert recovery_task is manager._recovery_task
    assert recovered.attempt_no == 2
    assert recovered.lease_owner == manager.executor_id
    await manager.shutdown()


@pytest.mark.asyncio
async def test_recovery_loop_start_is_idempotent() -> None:
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), _EmptyRepository(), _Sandbox(),
        recovery_scan_interval_seconds=0.01,
    )

    first = manager.start_recovery_loop()
    second = manager.start_recovery_loop()

    assert first is not None
    assert second is first
    assert manager._recovery_task is first
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_stops_recovery_loop_without_dangling_task() -> None:
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), _EmptyRepository(), _Sandbox(),
        recovery_scan_interval_seconds=0.01,
    )
    recovery_task = manager.start_recovery_loop()
    assert recovery_task is not None

    await manager.shutdown()

    assert manager._shutting_down is True
    assert recovery_task.done()
    assert manager.tasks == set()
    assert manager.start_recovery_loop() is None


@pytest.mark.asyncio
async def test_recovery_loop_continues_after_one_scan_failure(caplog) -> None:
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), _EmptyRepository(), _Sandbox(),
        recovery_scan_interval_seconds=0.01,
    )
    scans = 0
    second_scan = asyncio.Event()

    async def recover() -> int:
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("temporary database failure")
        second_scan.set()
        return 0

    manager.recover_stale_diagnoses = recover
    manager.start_recovery_loop()
    await asyncio.wait_for(second_scan.wait(), timeout=1)
    await manager.shutdown()

    assert scans >= 2
    assert "Diagnosis recovery scanner iteration failed" in caplog.text


@pytest.mark.asyncio
async def test_two_managers_rely_on_atomic_claim_for_one_stale_diagnosis() -> None:
    database, repository, session = _session()
    first = repository.claim(
        session.id, "crashed-executor", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="first attempt",
    )
    assert first is not None
    with database.connect() as connection:
        connection.execute(
            "UPDATE diagnosis_sessions SET lease_expires_at = ? WHERE id = ?",
            (_expired(), session.id),
        )
        connection.commit()

    manager_a = DiagnosisExecutionManager(_WaitingOrchestrator(), repository, _Sandbox())
    manager_b = DiagnosisExecutionManager(_WaitingOrchestrator(), repository, _Sandbox())
    submitted = await asyncio.gather(
        manager_a.recover_stale_diagnoses(), manager_b.recover_stale_diagnoses(),
    )
    assert submitted == [1, 1]
    for _ in range(50):
        await asyncio.sleep(0.01)
        claimed = repository.get_unscoped(session.id)
        if claimed.attempt_no == 2:
            break

    claimed = repository.get_unscoped(session.id)
    assert claimed.attempt_no == 2
    assert claimed.lease_owner in {manager_a.executor_id, manager_b.executor_id}
    assert sum(not task.done() for task in manager_a.tasks | manager_b.tasks) == 1
    await asyncio.gather(manager_a.shutdown(), manager_b.shutdown())


@pytest.mark.asyncio
async def test_graceful_shutdown_interrupts_without_business_cancellation() -> None:
    _, repository, session = _session()
    manager = DiagnosisExecutionManager(
        _WaitingOrchestrator(), repository, _Sandbox(),
        lease_ttl_seconds=60, heartbeat_interval_seconds=30, max_attempts=3,
    )
    manager.submit(session.id)
    for _ in range(30):
        await asyncio.sleep(0.01)
        running = repository.get_unscoped(session.id)
        if running and running.status.value == "INVESTIGATING":
            break
    await manager.shutdown()
    interrupted = repository.get_unscoped(session.id)
    assert interrupted.status.value == "INVESTIGATING"
    assert interrupted.lease_owner is None
    assert any(
        item.type == "diagnosis.interrupted"
        for item in repository.list_events(str(session.user_id), session.id)
    )


def test_same_idempotency_key_has_stable_sha256() -> None:
    first = DurableWorkflowRuntime.idempotency_key(
        WorkflowPhase.INVESTIGATE, "query_logs",
        {"b": 2, "a": "中文"}, ["ev-2", "ev-1"],
    )
    second = DurableWorkflowRuntime.idempotency_key(
        WorkflowPhase.INVESTIGATE, "query_logs",
        {"a": "中文", "b": 2}, ["ev-1", "ev-2"],
    )
    assert first == second
    assert len(first) == 64


@pytest.mark.asyncio
async def test_final_report_projection_and_completion_are_idempotent() -> None:
    database, repository, session = _session()
    claimed = repository.claim(
        session.id, "executor-a", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="report",
    )
    assert claimed is not None
    runtime = DurableWorkflowRuntime(repository, claimed, "executor-a", 60)

    class _Catalog:
        services = {"order-service": {"dependencies": [], "language": "Java"}}

        @staticmethod
        def resolve(_query):
            return "order-service"

    class _Workflow:
        catalog = _Catalog()

    evidence = Evidence(
        source="ELASTICSEARCH", source_type="ELASTICSEARCH", tool_name="query_logs", title="logs",
        detail="timeout", summary="timeout", structured_data={"services": ["order-service"]},
        timestamp=datetime.now(timezone.utc), evidence_id="final-evidence",
    )
    report = DiagnosisReport(
        query="timeout", run_id="report-run", service="order-service", symptom="5xx",
        environment="test", time_range="最近 30 分钟", conclusion="timeout",
        decision_summary="timeout", root_cause="upstream timeout", evidence=[evidence],
        root_cause_chain=["order-service", "timeout"], recommended_fix=["check upstream"],
        confidence=0.7, candidates=[], investigation_timeline=[], workflow_phases=[],
        context_compaction={},
    )
    request = DiagnosisCreateRequest(trigger_type="QUESTION", question="timeout")
    orchestrator = DiagnosisOrchestrator(_Workflow(), None, repository)

    affected_first = orchestrator._persist_report(session.id, report, request)
    affected_second = orchestrator._persist_report(session.id, report, request)
    await runtime.finalize_session(
        run_id=report.run_id, summary=report.decision_summary,
        affected_services=affected_first,
    )
    # 模拟 REPORT 结束后调用方重试；相同 logical run 应视为已成功。
    await runtime.finalize_session(
        run_id=report.run_id, summary=report.decision_summary,
        affected_services=affected_second,
    )

    with database.connect() as connection:
        root_count = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_root_causes WHERE diagnosis_id = ?",
            (session.id,),
        ).fetchone()
        report_steps = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_investigation_steps WHERE diagnosis_id = ? AND idempotency_key = 'final-report'",
            (session.id,),
        ).fetchone()
        completed_events = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_events WHERE diagnosis_id = ? AND event_key = 'diagnosis.completed'",
            (session.id,),
        ).fetchone()
    assert int(root_count["total"]) == 1
    assert int(report_steps["total"]) == 1
    assert int(completed_events["total"]) == 1


@pytest.mark.asyncio
async def test_startup_recovery_keeps_run_id_and_resumes_from_persisted_tools() -> None:
    database, repository, session = _session()
    first_session = repository.claim(
        session.id, "crashed-executor", lease_ttl_seconds=60, max_attempts=3,
        recovery_reason="first attempt",
    )
    assert first_session is not None
    first_runtime = DurableWorkflowRuntime(repository, first_session, "crashed-executor", 60)
    state = _state(session, "same-logical-run")
    state.phases = [WorkflowPhase.START, WorkflowPhase.TRIAGE, WorkflowPhase.INVESTIGATE]

    async def persist_tool(name, arguments):
        claim = await first_runtime.begin_tool(
            state, WorkflowPhase.INVESTIGATE, name, arguments, name, [],
        )
        evidence = Evidence(
            source="TEST", source_type="TEST", tool_name=name, title=name,
            detail=name, summary=name, structured_data={"services": ["order-service"]},
            timestamp=datetime.now(timezone.utc), evidence_id=claim.evidence_id,
        )
        record = ToolCallRecord(
            tool_name=name, arguments=arguments, result_summary=name,
            timestamp=datetime.now(timezone.utc), duration_ms=1, evidence_id=claim.evidence_id,
        )
        state.evidence.append(evidence)
        state.timeline.append(record)
        await first_runtime.complete_tool(
            state, WorkflowPhase.INVESTIGATE, claim, record, evidence, {"tool": name},
        )

    await persist_tool("tool_a", {"service": "order-service"})
    await persist_tool("tool_b", {"service": "payment-service"})
    repository.interrupt(session.id, "crashed-executor", "simulated process loss")

    class _ResumeOrchestrator:
        seen_run_id = None
        cached = []

        async def run(self, _user_id, _diagnosis_id, _request, *, resume_state, runtime):
            self.seen_run_id = resume_state.run_id
            for name, arguments in (
                ("tool_a", {"service": "order-service"}),
                ("tool_b", {"service": "payment-service"}),
            ):
                claim = await runtime.begin_tool(
                    resume_state, WorkflowPhase.INVESTIGATE, name, arguments, name, [],
                )
                self.cached.append(claim.completed)
            claim = await runtime.begin_tool(
                resume_state, WorkflowPhase.INVESTIGATE, "tool_c",
                {"service": "payment-db"}, "tool_c", [],
            )
            evidence = Evidence(
                source="TEST", source_type="TEST", tool_name="tool_c", title="tool_c",
                detail="tool_c", summary="tool_c", structured_data={},
                timestamp=datetime.now(timezone.utc), evidence_id=claim.evidence_id,
            )
            record = ToolCallRecord(
                tool_name="tool_c", arguments={"service": "payment-db"},
                result_summary="tool_c", timestamp=datetime.now(timezone.utc),
                duration_ms=1, evidence_id=claim.evidence_id,
            )
            resume_state.evidence.append(evidence)
            resume_state.timeline.append(record)
            await runtime.complete_tool(
                resume_state, WorkflowPhase.INVESTIGATE, claim, record, evidence,
                {"tool": "tool_c"},
            )
            await runtime.finalize_session(
                run_id=resume_state.run_id, summary="recovered",
                affected_services=["order-service", "payment-service"],
            )

    orchestrator = _ResumeOrchestrator()
    manager = DiagnosisExecutionManager(
        orchestrator, repository, _Sandbox(), lease_ttl_seconds=60,
        heartbeat_interval_seconds=30, max_attempts=3,
    )
    before = DiagnosisSelfCheckService(repository, manager).check(
        level=2, user_id=str(session.user_id),
    )
    assert any(issue.code == "MISSING_ACTIVE_LEASE" for issue in before.issues)
    assert await manager.recover_stale_diagnoses() == 1
    await asyncio.gather(*list(manager.tasks))

    completed = repository.get_unscoped(session.id)
    assert completed.status.value == "COMPLETED"
    assert completed.attempt_no == 2
    assert completed.run_id == "same-logical-run"
    assert orchestrator.seen_run_id == "same-logical-run"
    assert orchestrator.cached == [True, True]
    after = DiagnosisSelfCheckService(repository, manager).check(
        level=2, user_id=str(session.user_id),
    )
    assert after.status.value == "HEALTHY"
    with database.connect() as connection:
        step_count = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_investigation_steps WHERE diagnosis_id = ?",
            (session.id,),
        ).fetchone()
        evidence_count = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_evidence WHERE diagnosis_id = ?",
            (session.id,),
        ).fetchone()
        completed_events = connection.execute(
            "SELECT COUNT(*) AS total FROM diagnosis_events WHERE diagnosis_id = ? AND event_key = 'diagnosis.completed'",
            (session.id,),
        ).fetchone()
    assert int(step_count["total"]) == 3
    assert int(evidence_count["total"]) == 3
    assert int(completed_events["total"]) == 1
