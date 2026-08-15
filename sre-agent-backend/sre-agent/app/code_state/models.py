"""Code State 仅保存代码导航信息，不保存源码正文。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CodeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(max_length=120)
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    path: str = Field(max_length=500)
    symbol: str = Field(max_length=240)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class CodeComponent(BaseModel):
    """一个模块、配置文件、类或方法的短导航条目。"""

    model_config = ConfigDict(extra="forbid")

    module: str = Field(max_length=120)
    kind: Literal[
        "module", "manifest", "config", "controller", "service", "repository", "component"
    ]
    symbol: str = Field(max_length=240)
    path: str = Field(max_length=500)
    role: str = Field(max_length=160)
    relationships: list[str] = Field(default_factory=list, max_length=12)
    reference: CodeReference


class CodeRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=500)
    symbol: str = Field(max_length=240)
    role: str = Field(max_length=160)
    relationships: list[str] = Field(default_factory=list, max_length=12)


class CodeRolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CodeRoleUpdate] = Field(default_factory=list, max_length=40)
