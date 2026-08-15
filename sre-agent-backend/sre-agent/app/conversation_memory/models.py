"""短 Context State、Evidence 与压缩结果的数据契约。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShortContextState(BaseModel):
    """只保存继续完成任务必需的信息，历史事实进入 Memory Item。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(default="", max_length=120)
    user_intent: str = Field(default="", max_length=120)
    constraints: list[str] = Field(default_factory=list, max_length=5)
    confirmed_findings: list[str] = Field(default_factory=list, max_length=8)
    hypotheses: list[str] = Field(default_factory=list, max_length=5)
    decisions: list[str] = Field(default_factory=list, max_length=5)
    open_questions: list[str] = Field(default_factory=list, max_length=5)
    next_actions: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def enforce_short_items(self) -> "ShortContextState":
        limits = {
            "constraints": 80,
            "confirmed_findings": 120,
            "hypotheses": 120,
            "decisions": 120,
            "open_questions": 100,
            "next_actions": 100,
        }
        for field, limit in limits.items():
            if any(len(item) > limit for item in getattr(self, field)):
                raise ValueError(f"{field} contains an item longer than {limit} characters")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False)) > 4000:
            raise ValueError("context state exceeds 4000 characters")
        return self


class MemoryItemDraft(BaseModel):
    """写入大模型专用表的短记忆；Reference 只保存原始 Message ID。"""

    model_config = ConfigDict(extra="forbid")

    item_type: Literal[
        "goal", "user_intent", "constraint", "confirmed_finding", "hypothesis", "decision",
        "open_question", "next_action", "evidence", "reference",
    ]
    title: str = Field(max_length=60)
    content: str = Field(max_length=200)
    status: Literal["active", "resolved", "rejected", "superseded"] = "active"
    importance: float = Field(default=0.5, ge=0, le=1)
    source_message_id: str | None = Field(default=None, max_length=64)
    source_tool_name: str | None = Field(default=None, max_length=120)


class CompactionOutput(BaseModel):
    """一次 Compaction 必须整体通过校验后才能提交。"""

    model_config = ConfigDict(extra="forbid")

    conversation_summary: str = Field(max_length=1200)
    context_state: ShortContextState
    memory_items: list[MemoryItemDraft] = Field(default_factory=list, max_length=20)
