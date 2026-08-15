"""用户意图与工作流分流的数据契约。"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SREIntent(StrEnum):
    """进入任何诊断工具前必须确定的四种意图。"""

    SPECIFIC_INCIDENT = "SPECIFIC_INCIDENT"
    GENERAL_DIAGNOSIS = "GENERAL_DIAGNOSIS"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class IntentDecision(BaseModel):
    """LLM Router 唯一允许返回的结构。"""

    model_config = ConfigDict(extra="forbid")

    intent: SREIntent
    target: str | None = Field(default=None, max_length=120)
    symptom: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_scope(self) -> "IntentDecision":
        """具体故障缺少对象或现象时不能越过意图闸门。"""
        if self.intent is SREIntent.SPECIFIC_INCIDENT and (not self.target or not self.symptom):
            raise ValueError("SPECIFIC_INCIDENT requires both target and symptom")
        if self.intent in {SREIntent.NEED_CLARIFICATION, SREIntent.OUT_OF_SCOPE}:
            if self.target is not None or self.symptom is not None:
                raise ValueError(f"{self.intent.value} requires null target and symptom")
        return self


class IntentReply(BaseModel):
    """不应进入诊断工作流时返回给 API 和前端的普通消息。"""

    type: Literal["message"] = "message"
    intent: SREIntent
    message: str
    conversation_id: str | None = None
    target: str | None = None
    symptom: str | None = None
