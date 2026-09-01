"""Diagnosis Session、Timeline、Evidence、Graph 与 Root Cause 数据契约。"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DiagnosisTriggerType(StrEnum):
    QUESTION = "QUESTION"
    SERVICE = "SERVICE"
    POD = "POD"


class DiagnosisTargetType(StrEnum):
    SERVICE = "SERVICE"
    POD = "POD"


class DiagnosisStatus(StrEnum):
    PENDING = "PENDING"
    INVESTIGATING = "INVESTIGATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InvestigationStepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DiagnosisTarget(BaseModel):
    type: DiagnosisTargetType
    namespace: str = Field(default="sre-lab", min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)


class InvestigationStep(BaseModel):
    id: str
    diagnosis_id: str
    sequence_no: int
    step_type: str
    target_type: str | None = None
    target_id: str | None = None
    tool_name: str | None = None
    status: InvestigationStepStatus
    started_at: str
    finished_at: str | None = None
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None


class DiagnosisEvidence(BaseModel):
    id: str
    diagnosis_id: str
    source_type: str
    source_name: str
    resource_type: str | None = None
    resource_id: str | None = None
    title: str
    summary: str
    raw_data: Any = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    supports_conclusion: bool = True
    timestamp: str


class IncidentGraphNode(BaseModel):
    id: str
    type: str
    name: str
    status: str = "UNKNOWN"
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentGraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str
    latency_ms: float | None = None
    status: str = "UNKNOWN"
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentGraph(BaseModel):
    nodes: list[IncidentGraphNode] = Field(default_factory=list)
    edges: list[IncidentGraphEdge] = Field(default_factory=list)


class RootCauseResource(BaseModel):
    type: str
    name: str


class DiagnosisRootCause(BaseModel):
    title: str
    description: str
    root_resource: RootCauseResource | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class DiagnosisSession(BaseModel):
    id: str
    conversation_id: str
    run_id: str | None = None
    question: str
    trigger_type: DiagnosisTriggerType
    initial_target: DiagnosisTarget | None = None
    status: DiagnosisStatus
    summary: str | None = None
    affected_services: list[str] = Field(default_factory=list)
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str
    updated_at: str
    root_cause: DiagnosisRootCause | None = None
    steps: list[InvestigationStep] = Field(default_factory=list)
    evidence: list[DiagnosisEvidence] = Field(default_factory=list)
    graph: IncidentGraph = Field(default_factory=IncidentGraph)


class DiagnosisEvent(BaseModel):
    id: int
    diagnosis_id: str
    type: str
    data: dict[str, Any]
    created_at: str
