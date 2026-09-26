"""MySQL-backed durable Diagnosis executor, lease owner and Workflow Runtime adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.conversation_memory import conversation_memory_scope
from app.diagnosis.models import DiagnosisEvidence, DiagnosisSession
from app.diagnosis.repository import DiagnosisOwnershipLost, DiagnosisRepository
from app.diagnosis.schemas import DiagnosisCreateRequest
from app.sandbox import DockerSandboxManager
from app.security import task_security_scope
from app.workflow.models import DiagnosisState, Evidence, ToolCallRecord, WorkflowPhase
from app.workflow.runtime import PendingToolCall, ToolExecutionClaim, WorkflowRuntime

logger = logging.getLogger(__name__)


class DurableWorkflowRuntime(WorkflowRuntime):
    """把一个 Workflow 的逻辑操作映射为 MySQL Checkpoint 与幂等 Step。"""

    _ORDER = [
        WorkflowPhase.START, WorkflowPhase.SYSTEM_SCAN, WorkflowPhase.TRIAGE,
        WorkflowPhase.BASELINE_OBSERVATION, WorkflowPhase.ANALYZE,
        WorkflowPhase.INVESTIGATE, WorkflowPhase.VERIFY,
        WorkflowPhase.REPORT, WorkflowPhase.END,
    ]

    def __init__(
        self,
        repository: DiagnosisRepository,
        session: DiagnosisSession,
        lease_owner: str,
        lease_ttl_seconds: float,
    ) -> None:
        self.repository = repository
        self.diagnosis_id = session.id
        self.lease_owner = lease_owner
        self.lease_ttl_seconds = lease_ttl_seconds
        self.state_version = session.state_version
        self.resume_phase = session.current_phase
        self.resume_phase_status = session.phase_status.value
        self.current_phase = session.current_phase or WorkflowPhase.START.value
        self._has_checkpoint = bool(session.checkpoint_json)
        self._lock = asyncio.Lock()
        self._interrupted_steps = repository.list_running_steps_unscoped(session.id)
        self._forced_keys = {
            self._tool_signature(step.tool_name or "", step.arguments): step.idempotency_key
            for step in self._interrupted_steps if step.idempotency_key
        }

    async def initialize(self, state: DiagnosisState) -> None:
        """在任何 Conversation/Tool 副作用前 durable 保存首个 logical run_id。"""
        if self._has_checkpoint:
            return
        if WorkflowPhase.START not in state.phases:
            state.phases.append(WorkflowPhase.START)
        async with self._lock:
            self.state_version = self.repository.save_checkpoint(
                self.diagnosis_id, self.lease_owner, self.state_version,
                state.model_dump(mode="json"), current_phase=WorkflowPhase.START.value,
                phase_status="PENDING", lease_ttl_seconds=self.lease_ttl_seconds,
                event_type="checkpoint.saved", event_key="checkpoint.initialized",
            )
            self._has_checkpoint = True

    async def persist_projection(self, writer):
        async with self._lock:
            with self.repository.owned_projection(self.diagnosis_id, self.lease_owner, self.state_version):
                return writer()

    def should_skip_phase(self, phase: WorkflowPhase) -> bool:
        if not self.resume_phase:
            return False
        try:
            resume_index = [item.value for item in self._ORDER].index(self.resume_phase)
            phase_index = self._ORDER.index(phase)
        except ValueError:
            return False
        return phase_index < resume_index or (
            phase_index == resume_index and self.resume_phase_status == "COMPLETED"
        )

    def pending_tool_calls(self, phase: WorkflowPhase) -> list[PendingToolCall]:
        if phase is not WorkflowPhase.INVESTIGATE or self.resume_phase != phase.value:
            return []
        calls = [PendingToolCall(
            tool_name=step.tool_name or "", arguments=step.arguments,
            title=f"恢复中断步骤：{step.tool_name or 'unknown'}",
            parent_evidence_ids=step.parent_evidence_ids,
        ) for step in self._interrupted_steps if step.tool_name]
        self._interrupted_steps = []
        return calls

    async def phase_started(self, state: DiagnosisState, phase: WorkflowPhase) -> None:
        async with self._lock:
            self.current_phase = phase.value
            self.state_version = self.repository.save_checkpoint(
                self.diagnosis_id, self.lease_owner, self.state_version,
                state.model_dump(mode="json"), current_phase=phase.value,
                phase_status="RUNNING", lease_ttl_seconds=self.lease_ttl_seconds,
                event_type="phase.changed", event_key=f"phase.started:{phase.value}",
            )

    async def phase_completed(self, state: DiagnosisState, phase: WorkflowPhase) -> None:
        async with self._lock:
            self.current_phase = phase.value
            self.state_version = self.repository.save_checkpoint(
                self.diagnosis_id, self.lease_owner, self.state_version,
                state.model_dump(mode="json"), current_phase=phase.value,
                phase_status="COMPLETED", lease_ttl_seconds=self.lease_ttl_seconds,
                event_type="phase.completed", event_key=f"phase.completed:{phase.value}",
            )

    async def checkpoint(
        self, state: DiagnosisState, phase: WorkflowPhase, operation_key: str,
    ) -> None:
        async with self._lock:
            self.state_version = self.repository.save_checkpoint(
                self.diagnosis_id, self.lease_owner, self.state_version,
                state.model_dump(mode="json"), current_phase=phase.value,
                phase_status="RUNNING", lease_ttl_seconds=self.lease_ttl_seconds,
                event_type="checkpoint.saved",
                event_key=f"checkpoint.runtime:{operation_key}",
            )

    async def begin_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        parent_evidence_ids: list[str],
    ) -> ToolExecutionClaim:
        del title
        signature = self._tool_signature(tool_name, arguments)
        key = self._forced_keys.pop(
            signature, self.idempotency_key(phase, tool_name, arguments, parent_evidence_ids),
        )
        evidence_id = hashlib.sha256(
            f"{self.diagnosis_id}:{key}:evidence".encode("utf-8")
        ).hexdigest()[:32]
        target_type, target_id = self._tool_target(arguments, state.service)
        async with self._lock:
            step, completed, version = self.repository.begin_tool_step(
                self.diagnosis_id, self.lease_owner, self.state_version,
                idempotency_key=key, evidence_id=evidence_id, tool_name=tool_name,
                arguments=arguments, parent_evidence_ids=parent_evidence_ids,
                target_type=target_type, target_id=target_id,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self.state_version = version
        existing_evidence = next(
            (item for item in state.evidence if item.evidence_id == evidence_id), None
        )
        if completed and existing_evidence is None:
            persisted = self.repository.get_evidence_unscoped(self.diagnosis_id, evidence_id)
            if persisted is not None:
                existing_evidence = self._workflow_evidence(persisted)
        existing_record = next(
            (item for item in state.timeline if item.tool_name == tool_name and item.arguments == arguments), None
        )
        if completed and existing_record is None:
            existing_record = ToolCallRecord(
                tool_name=tool_name, arguments=arguments, result_summary=step.summary,
                timestamp=step.started_at, duration_ms=0, error=step.error_message,
                evidence_id=step.evidence_id,
            )
        return ToolExecutionClaim(
            step_id=step.id,
            idempotency_key=key, evidence_id=evidence_id, completed=completed,
            result=step.result, evidence=existing_evidence, record=existing_record,
        )

    async def complete_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        claim: ToolExecutionClaim,
        record: ToolCallRecord,
        evidence: Evidence | None,
        result: Any,
    ) -> None:
        if claim.idempotency_key is None:
            return
        persisted_evidence = self._diagnosis_evidence(
            evidence, result, claim.idempotency_key,
        ) if evidence is not None else None
        async with self._lock:
            self.state_version = self.repository.complete_tool_step(
                self.diagnosis_id, self.lease_owner, self.state_version,
                idempotency_key=claim.idempotency_key, result_value=result,
                step_status="FAILED" if record.error else "COMPLETED",
                summary=record.result_summary or record.error or "工具未返回摘要",
                error_message=record.error, evidence=persisted_evidence,
                checkpoint=state.model_dump(mode="json"), current_phase=phase.value,
                lease_ttl_seconds=self.lease_ttl_seconds,
            )

    async def heartbeat(self) -> None:
        async with self._lock:
            self.state_version = self.repository.heartbeat(
                self.diagnosis_id, self.lease_owner, self.state_version,
                self.lease_ttl_seconds,
            )

    async def finalize_session(
        self, *, run_id: str, summary: str, affected_services: list[str],
    ) -> None:
        async with self._lock:
            self.state_version = self.repository.complete_owned_session(
                self.diagnosis_id, self.lease_owner, self.state_version,
                run_id=run_id, summary=summary, affected_services=affected_services,
            )

    @staticmethod
    def idempotency_key(
        phase: WorkflowPhase, tool_name: str, arguments: dict[str, Any],
        parent_evidence_ids: list[str],
    ) -> str:
        canonical_arguments = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        payload = json.dumps(
            [phase.value, tool_name, canonical_arguments, sorted(parent_evidence_ids)],
            separators=(",", ":"), ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_signature(tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'), ensure_ascii=False)}"

    def _diagnosis_evidence(
        self, evidence: Evidence, result: Any, idempotency_key: str,
    ) -> DiagnosisEvidence:
        structured = evidence.structured_data
        pods = structured.get("pods", [])
        services = structured.get("services", [])
        return DiagnosisEvidence(
            id=evidence.evidence_id, diagnosis_id=self.diagnosis_id,
            source_type=(evidence.source_type or evidence.source or "TOOL").upper(),
            source_name=evidence.tool_name,
            resource_type="POD" if pods else (
                "DATABASE" if evidence.tool_name in {"query_slow_queries", "query_sql_digest", "explain_sql"}
                else "SERVICE"
            ),
            resource_id=str(pods[0]) if pods else (
                str(services[0]) if services else None
            ),
            title=evidence.title, summary=evidence.summary or evidence.detail,
            raw_data={"result": result, "structured_data": structured},
            metadata={
                "observability": {key: getattr(evidence, key) for key in (
                    "evidence_type", "service_name", "trace_id", "severity", "raw_reference")},
                "logical_step_key": idempotency_key,
                "parent_evidence_ids": evidence.parent_evidence_ids,
                "source_references": [item.model_dump(mode="json") for item in evidence.source_references],
                "direct_evidence": evidence.direct_evidence,
            },
            supports_conclusion=evidence.supports_conclusion,
            timestamp=evidence.timestamp.isoformat(),
        )

    @staticmethod
    def _workflow_evidence(evidence: DiagnosisEvidence) -> Evidence:
        """把原子提交的领域 Evidence 重新放回恢复后的 Workflow State。"""
        raw_data = evidence.raw_data if isinstance(evidence.raw_data, dict) else {}
        structured = raw_data.get("structured_data", {})
        metadata = evidence.metadata
        from app.workflow.evidence_gate import source_for_tool
        return Evidence(
            source=source_for_tool(evidence.source_name), source_type=evidence.source_type,
            **{key: value for key, value in metadata.get("observability", {}).items()
               if key in {"evidence_type", "service_name", "trace_id", "severity", "raw_reference"}},
            tool_name=evidence.source_name, title=evidence.title,
            detail=evidence.summary, summary=evidence.summary,
            structured_data=structured if isinstance(structured, dict) else {},
            timestamp=datetime.fromisoformat(evidence.timestamp.replace("Z", "+00:00")),
            evidence_id=evidence.id,
            parent_evidence_ids=[str(item) for item in metadata.get("parent_evidence_ids", [])],
            source_references=metadata.get("source_references", []),
            supports_conclusion=evidence.supports_conclusion,
            direct_evidence=bool(metadata.get("direct_evidence", False)),
        )

    @staticmethod
    def _tool_target(arguments: dict[str, Any], fallback: str) -> tuple[str | None, str | None]:
        for key, resource_type in (
            ("pod", "POD"), ("pod_name", "POD"),
            ("service", "SERVICE"), ("service_name", "SERVICE"),
        ):
            if arguments.get(key):
                return resource_type, str(arguments[key])
        return ("SERVICE", fallback) if fallback and fallback != "unknown" else (None, None)


class DiagnosisExecutionManager:
    """进程内 Executor；MySQL Session 而非 asyncio Task 是 durable source of truth。"""

    def __init__(
        self,
        orchestrator: Any,
        repository: DiagnosisRepository,
        sandbox: DockerSandboxManager,
        *,
        lease_ttl_seconds: float = 60,
        heartbeat_interval_seconds: float = 10,
        recovery_scan_interval_seconds: float = 15,
        recovery_batch_size: int = 20,
        max_attempts: int = 3,
    ) -> None:
        self.orchestrator = orchestrator
        self.repository = repository
        self.sandbox = sandbox
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.recovery_scan_interval_seconds = recovery_scan_interval_seconds
        self.recovery_batch_size = max(1, min(200, int(recovery_batch_size)))
        self.max_attempts = max_attempts
        self.executor_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self.tasks: set[asyncio.Task[None]] = set()
        self._submitted_diagnoses: set[str] = set()
        self._recovery_task: asyncio.Task[None] | None = None
        self._heartbeat_diagnoses: set[str] = set()
        self._shutting_down = False

    def submit(self, diagnosis_id: str, *, reason: str = "api submission") -> asyncio.Task[None] | None:
        if self._shutting_down or diagnosis_id in self._submitted_diagnoses:
            return None
        self._submitted_diagnoses.add(diagnosis_id)
        task = asyncio.create_task(
            self._execute(diagnosis_id, reason), name=f"diagnosis-{diagnosis_id}",
        )
        self.tasks.add(task)
        task.add_done_callback(
            lambda completed, current_id=diagnosis_id: self._submission_finished(
                current_id, completed,
            )
        )
        return task

    def _submission_finished(
        self, diagnosis_id: str, task: asyncio.Task[None],
    ) -> None:
        self.tasks.discard(task)
        self._submitted_diagnoses.discard(diagnosis_id)

    async def recover_stale_diagnoses(self) -> int:
        # 一个 scanner tick 只读取并提交一批；剩余任务留给下一个 interval，
        # 避免积压时在同一轮创建无界数量的 asyncio Task。
        sessions = self.repository.list_recoverable(limit=self.recovery_batch_size)
        recovered = 0
        for session in sessions:
            if session.attempt_no >= self.max_attempts:
                self.repository.mark_max_attempts_exceeded(session.id, self.max_attempts)
                continue
            if self.submit(session.id, reason="startup recovery") is not None:
                recovered += 1
        return recovered

    def start_recovery_loop(self) -> asyncio.Task[None] | None:
        """幂等启动周期扫描；应用 lifespan 负责决定首次启动时机。"""
        if self._shutting_down:
            return None
        if self._recovery_task is not None and not self._recovery_task.done():
            return self._recovery_task
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(), name="diagnosis-recovery-scanner",
        )
        return self._recovery_task

    async def _recovery_loop(self) -> None:
        logger.info(
            "Diagnosis recovery scanner started: interval_seconds=%s batch_size=%s",
            self.recovery_scan_interval_seconds, self.recovery_batch_size,
        )
        while not self._shutting_down:
            try:
                await asyncio.sleep(self.recovery_scan_interval_seconds)
                if self._shutting_down:
                    break
                recovered = await self.recover_stale_diagnoses()
                if recovered:
                    logger.warning(
                        "Recovery scanner submitted %s stale Diagnosis Session(s)",
                        recovered,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Diagnosis recovery scanner iteration failed")

    async def shutdown(self) -> None:
        self._shutting_down = True
        recovery_task = self._recovery_task
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _execute(self, diagnosis_id: str, reason: str) -> None:
        session = self.repository.claim(
            diagnosis_id, self.executor_id,
            lease_ttl_seconds=self.lease_ttl_seconds,
            max_attempts=self.max_attempts, recovery_reason=reason,
        )
        if session is None:
            return
        runtime = DurableWorkflowRuntime(
            self.repository, session, self.executor_id, self.lease_ttl_seconds,
        )
        try:
            resume_state = (
                DiagnosisState.model_validate_json(session.checkpoint_json)
                if session.checkpoint_json else None
            )
        except Exception as exc:
            self.repository.fail_owned_session(
                diagnosis_id, self.executor_id, f"invalid checkpoint: {exc}",
            )
            return

        request = DiagnosisCreateRequest(
            trigger_type=session.trigger_type,
            question=session.question,
            initial_target=session.initial_target,
            project_id=session.project_id,
        )
        heartbeat_task = asyncio.create_task(self._heartbeat(runtime), name=f"heartbeat-{diagnosis_id}")
        try:
            if session.attempt_no > 1:
                await runtime.persist_projection(lambda: self.repository.append_event(
                    diagnosis_id, "diagnosis.recovered",
                    {"diagnosis_id": diagnosis_id, "attempt_no": session.attempt_no, "run_id": session.run_id},
                    event_key=f"diagnosis.recovered:{session.attempt_no}",
                ))
            task_id = f"{diagnosis_id}-{session.attempt_no}-{uuid4().hex[:8]}"
            async with self.sandbox.task_workspace(task_id) as workspace:
                with task_security_scope(
                    str(session.user_id), session.project_id, task_id, str(workspace),
                ):
                    with conversation_memory_scope(str(session.user_id), session.conversation_id):
                        work = asyncio.create_task(self.orchestrator.run(
                            str(session.user_id), diagnosis_id, request,
                            resume_state=resume_state, runtime=runtime,
                        ))
                        try:
                            done, _ = await asyncio.wait(
                                {work, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED,
                            )
                            if heartbeat_task in done:
                                # A failed heartbeat must stop the model/tool await immediately.
                                await heartbeat_task
                            await work
                        finally:
                            if not work.done():
                                work.cancel()
                            await asyncio.gather(work, return_exceptions=True)
        except asyncio.CancelledError:
            self.repository.interrupt(diagnosis_id, self.executor_id, "application shutdown")
            raise
        except DiagnosisOwnershipLost:
            logger.warning("Diagnosis lease ownership lost: diagnosis_id=%s", diagnosis_id)
        except Exception as exc:
            logger.exception("durable diagnosis failed: diagnosis_id=%s", diagnosis_id)
            detail = str(exc).strip()
            message = f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__
            self.repository.fail_owned_session(diagnosis_id, self.executor_id, message)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat(self, runtime: DurableWorkflowRuntime) -> None:
        self._heartbeat_diagnoses.add(runtime.diagnosis_id)
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                await runtime.heartbeat()
        finally:
            self._heartbeat_diagnoses.discard(runtime.diagnosis_id)

    @property
    def heartbeat_running(self) -> bool:
        return bool(self._heartbeat_diagnoses)

    @property
    def heartbeat_subsystem_ready(self) -> bool:
        """没有活动 Diagnosis 时无需起空转心跳；未关机即表示子系统可用。"""
        return not self._shutting_down
