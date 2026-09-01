"""Diagnosis HTTP API 请求与轻量响应模型。"""

from pydantic import BaseModel, Field, model_validator

from app.diagnosis.models import DiagnosisSession, DiagnosisStatus, DiagnosisTarget, DiagnosisTriggerType


class DiagnosisCreateRequest(BaseModel):
    trigger_type: DiagnosisTriggerType
    question: str = Field(default="诊断当前资源异常", min_length=1, max_length=20_000)
    initial_target: DiagnosisTarget | None = None
    project_id: str = Field(default="sre-lab", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")

    @model_validator(mode="after")
    def validate_trigger_target(self) -> "DiagnosisCreateRequest":
        if self.trigger_type is DiagnosisTriggerType.QUESTION and self.initial_target is not None:
            raise ValueError("QUESTION diagnosis must not require initial_target")
        if self.trigger_type in {DiagnosisTriggerType.SERVICE, DiagnosisTriggerType.POD}:
            if self.initial_target is None or self.initial_target.type.value != self.trigger_type.value:
                raise ValueError(f"{self.trigger_type.value} diagnosis requires matching initial_target")
        return self


class DiagnosisCreatedResponse(BaseModel):
    id: str
    status: DiagnosisStatus
    events_url: str
    detail_url: str


class QuickDiagnosisRequest(BaseModel):
    """服务详情页的一次性快速诊断；不创建 Conversation 或 Diagnosis Session。"""

    question: str = Field(default="快速诊断当前资源异常", min_length=1, max_length=20_000)
    target: DiagnosisTarget
    project_id: str = Field(default="sre-lab", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


class DiagnosisListResponse(BaseModel):
    items: list[DiagnosisSession]
