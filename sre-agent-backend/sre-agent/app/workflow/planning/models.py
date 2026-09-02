"""Planner 对外稳定的数据契约。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlannerDecision(BaseModel):
    """单轮 Planner 决策；参数仍会经过 Tool Policy 二次校验。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["tool", "finish", "insufficient_evidence"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(default="下一步调查", max_length=120)
    reason: str = Field(default="", max_length=500)
    parent_evidence_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_action(self) -> "PlannerDecision":
        if self.action == "tool" and not self.tool_name:
            raise ValueError("tool action requires tool_name")
        if self.action != "tool" and (self.tool_name is not None or self.arguments):
            raise ValueError("non-tool action cannot contain tool_name or arguments")
        return self
