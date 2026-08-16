"""Tool Policy、项目授权和任务身份的数据契约。"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True, slots=True)
class ToolRule:
    name: str
    category: str
    risk: ToolRisk
    schema: dict


@dataclass(frozen=True, slots=True)
class ProjectPolicy:
    project_id: str
    namespace: str
    repositories: dict[str, tuple[Path, ...]]
    enabled_tools: frozenset[str]


@dataclass(frozen=True, slots=True)
class TaskSecurityScope:
    user_id: str
    project_id: str
    task_id: str
    workspace: str | None = None
