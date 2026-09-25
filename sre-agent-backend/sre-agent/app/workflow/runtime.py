"""DiagnosisWorkflow 的可选持久执行边界。

Quick Diagnosis 使用 No-op；持久 Diagnosis 由 diagnosis.execution 提供 MySQL 实现。
该接口只保存外部可验证状态，不保存模型隐藏 Chain-of-Thought。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.workflow.models import DiagnosisState, Evidence, ToolCallRecord, WorkflowPhase


@dataclass(slots=True)
class ToolExecutionClaim:
    idempotency_key: str | None = None
    evidence_id: str | None = None
    completed: bool = False
    result: Any = None
    evidence: Evidence | None = None
    record: ToolCallRecord | None = None
    step_id: str | None = None


@dataclass(slots=True)
class PendingToolCall:
    """上一个 Executor 在 Tool commit 前中断的 logical operation。"""

    tool_name: str
    arguments: dict[str, Any]
    title: str
    parent_evidence_ids: list[str]


class WorkflowRuntime(Protocol):
    async def persist_projection(self, writer: Callable[[], Any]) -> Any: ...

    async def initialize(self, state: DiagnosisState) -> None: ...

    def should_skip_phase(self, phase: WorkflowPhase) -> bool: ...

    def pending_tool_calls(self, phase: WorkflowPhase) -> list[PendingToolCall]: ...

    async def phase_started(self, state: DiagnosisState, phase: WorkflowPhase) -> None: ...

    async def phase_completed(self, state: DiagnosisState, phase: WorkflowPhase) -> None: ...

    async def checkpoint(
        self, state: DiagnosisState, phase: WorkflowPhase, operation_key: str,
    ) -> None: ...

    async def begin_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        parent_evidence_ids: list[str],
    ) -> ToolExecutionClaim: ...

    async def complete_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        claim: ToolExecutionClaim,
        record: ToolCallRecord,
        evidence: Evidence | None,
        result: Any,
    ) -> None: ...

    async def finalize_session(
        self, *, run_id: str, summary: str, affected_services: list[str],
    ) -> None: ...


class NoopWorkflowRuntime:
    """Quick Diagnosis 与旧调用路径保持无数据库 Checkpoint。"""

    async def persist_projection(self, writer: Callable[[], Any]) -> Any:
        return writer()

    async def initialize(self, state: DiagnosisState) -> None:
        del state

    def should_skip_phase(self, phase: WorkflowPhase) -> bool:
        del phase
        return False

    def pending_tool_calls(self, phase: WorkflowPhase) -> list[PendingToolCall]:
        del phase
        return []

    async def phase_started(self, state: DiagnosisState, phase: WorkflowPhase) -> None:
        del state, phase

    async def phase_completed(self, state: DiagnosisState, phase: WorkflowPhase) -> None:
        del state, phase

    async def checkpoint(
        self, state: DiagnosisState, phase: WorkflowPhase, operation_key: str,
    ) -> None:
        del state, phase, operation_key

    async def begin_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        tool_name: str,
        arguments: dict[str, Any],
        title: str,
        parent_evidence_ids: list[str],
    ) -> ToolExecutionClaim:
        del state, phase, tool_name, arguments, title, parent_evidence_ids
        return ToolExecutionClaim()

    async def complete_tool(
        self,
        state: DiagnosisState,
        phase: WorkflowPhase,
        claim: ToolExecutionClaim,
        record: ToolCallRecord,
        evidence: Evidence | None,
        result: Any,
    ) -> None:
        del state, phase, claim, record, evidence, result

    async def finalize_session(
        self, *, run_id: str, summary: str, affected_services: list[str],
    ) -> None:
        del run_id, summary, affected_services
