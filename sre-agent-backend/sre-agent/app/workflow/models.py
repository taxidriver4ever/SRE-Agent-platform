"""诊断状态、证据、候选原因和统一报告的数据契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.evidence import SourceReference


class WorkflowPhase(StrEnum):
    """硬性工作流阶段；顺序与任务要求完全一致。"""

    START = "START"
    TRIAGE = "TRIAGE"
    BASELINE_OBSERVATION = "BASELINE_OBSERVATION"
    ANALYZE = "ANALYZE"
    INVESTIGATE = "INVESTIGATE"
    VERIFY = "VERIFY"
    REPORT = "REPORT"
    END = "END"


class ToolCallRecord(BaseModel):
    """一次工具调用的可审计记录。"""

    tool_name: str
    arguments: dict[str, Any]
    result_summary: str = ""
    timestamp: datetime
    duration_ms: int
    error: str | None = None
    # 每次成功调用都指向 Evidence Store 中的完整原始结果。
    evidence_id: str | None = None


class Evidence(BaseModel):
    """从真实系统取得的一条证据，而不是模型猜测。"""

    source: str
    tool_name: str
    title: str
    detail: str
    timestamp: datetime
    evidence_id: str
    source_references: list[SourceReference] = Field(default_factory=list)
    # 空查询仍保留在时间线中，但不能被 VERIFY 当作支持根因的独立证据。
    supports_conclusion: bool = True


class CandidateCause(BaseModel):
    """ANALYZE 阶段产生的待验证候选原因。"""

    cause: str
    reason: str
    priority: int = Field(ge=1, le=5)


class DiagnosisReport(BaseModel):
    """前端与评测统一消费的最终报告结构。"""

    query: str
    run_id: str
    conversation_id: str | None = None
    service: str
    affected_pod: str | None = None
    language: str = "unknown"
    running_version: str | None = None
    git_sha: str | None = None
    source_code_location: str | None = None
    repository_url: str | None = None
    symptom: str
    environment: str
    time_range: str
    conclusion: str
    # Ollama 经统一 Gateway 生成的简短决策摘要；根因字段仍由证据门槛约束。
    decision_summary: str
    root_cause: str
    evidence: list[Evidence]
    root_cause_chain: list[str]
    recommended_fix: list[str]
    confidence: float = Field(ge=0, le=1)
    candidates: list[CandidateCause]
    investigation_timeline: list[ToolCallRecord]
    workflow_phases: list[WorkflowPhase]
    # 说明送入 LLM 的是活动上下文，完整消息与 Tool Result 保存在 MySQL。
    context_compaction: dict[str, int | str]


class DiagnosisState(BaseModel):
    """工作流节点间共享的显式状态。"""

    query: str
    # Conversation ID 把多次诊断汇入同一组持久化 Summary、State 和 Memory。
    conversation_id: str = ""
    user_id: str | None = None
    run_id: str = ""
    service: str = "unknown"
    symptom: str = "待确定"
    environment: str = "local-kind/sre-lab"
    time_range_minutes: int = 30
    pod_name: str | None = None
    runtime_commit: str | None = None
    language: str = "unknown"
    repository: str | None = None
    repository_url: str | None = None
    source_code_location: str | None = None
    pod_versions: dict[str, str] = Field(default_factory=dict)
    mixed_versions: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    candidates: list[CandidateCause] = Field(default_factory=list)
    timeline: list[ToolCallRecord] = Field(default_factory=list)
    phases: list[WorkflowPhase] = Field(default_factory=list)
    llm_decision_summary: str = ""
