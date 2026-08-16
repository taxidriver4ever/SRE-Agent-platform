"""任务 Workspace 与未来执行型 Tool 的 Docker 强制隔离层。"""

from __future__ import annotations

import re
import shutil
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app.core.process import run_fixed_command


_TASK_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class SandboxError(RuntimeError):
    pass


class DockerSandboxManager:
    """为任务创建独立目录，并为代码执行构造固定 Docker 安全参数。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        image: str,
        cpus: float = 1.0,
        memory_mb: int = 512,
        pids_limit: int = 128,
        timeout_seconds: float = 120,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.image = image
        self.cpus = max(0.1, min(float(cpus), 8.0))
        self.memory_mb = max(64, min(int(memory_mb), 8192))
        self.pids_limit = max(16, min(int(pids_limit), 1024))
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 3600.0))

    @asynccontextmanager
    async def task_workspace(self, task_id: str) -> AsyncIterator[Path]:
        """只读阶段也建立任务目录；退出时只删除已验证的任务子目录。"""
        workspace = self._workspace(task_id)
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            yield workspace
        finally:
            resolved = workspace.resolve()
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError as exc:
                raise SandboxError("refusing to remove workspace outside sandbox root") from exc
            if resolved != self.workspace_root and resolved.exists():
                shutil.rmtree(resolved)

    def docker_arguments(self, task_id: str, command: Sequence[str]) -> list[str]:
        """返回不经过 shell 的固定 Docker argv；模型不能改变安全选项。"""
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise SandboxError("sandbox command must be a non-empty argv sequence")
        workspace = self._workspace(task_id)
        return [
            "run", "--rm",
            "--name", f"sre-agent-{task_id.lower()}",
            "--network", "none",
            "--cpus", str(self.cpus),
            "--memory", f"{self.memory_mb}m",
            "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", f"type=bind,source={workspace},target=/workspace",
            "--workdir", "/workspace",
            self.image,
            *command,
        ]

    async def run(self, task_id: str, command: Sequence[str]) -> str:
        """仅供未来受审核的 CodeExecuteTool 调用；当前不注册为模型 Tool。"""
        workspace = self._workspace(task_id)
        if not workspace.is_dir():
            raise SandboxError("task workspace is not active")
        return await run_fixed_command(
            "docker",
            self.docker_arguments(task_id, command),
            timeout=self.timeout_seconds,
        )

    def _workspace(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(task_id):
            raise SandboxError("invalid task_id")
        workspace = (self.workspace_root / task_id).resolve()
        try:
            workspace.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SandboxError("task workspace escapes sandbox root") from exc
        if workspace == self.workspace_root:
            raise SandboxError("task workspace cannot equal sandbox root")
        return workspace
