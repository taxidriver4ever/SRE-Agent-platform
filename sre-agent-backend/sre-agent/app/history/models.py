"""Bounded case projections and search contracts, separate from live state."""
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator


class HistoryItem(BaseModel):
    history_id: str
    task_id: str
    user_id: str
    project_id: str
    service: str
    timestamp: datetime
    category: str = "unknown"
    symptom: str = Field(max_length=600)
    root_cause: str = Field(max_length=1200)
    summary: str = Field(max_length=1200)
    severity: str = "UNKNOWN"
    tags: list[str] = Field(default_factory=list, max_length=12)
    evidence_summary: list[str] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    conclusion_status: str = "insufficient_evidence"


class HistoryQuery(BaseModel):
    service: str = Field(min_length=1, max_length=160)
    text: str = Field(default="", max_length=600)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=5, ge=1, le=10)
    exclude_task_id: str | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("history timestamps require timezone")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("invalid history time range")
        return self
