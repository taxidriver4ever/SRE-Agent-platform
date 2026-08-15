"""把运行镜像 Git SHA 映射回提交、差异与源码。"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.mcp_servers.common import bounded, run_fixed_command
from app.repositories.registry import RepositoryRegistry

# 允许 Git 常用的父提交语法 HEAD^ / HEAD~1；仍禁止空白、冒号和以 '-' 开头的选项。
_REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/^~-]{0,199}$")


class GitReadBackend:
    """FastMCP 工具背后的 Git 白名单只读操作执行器。"""

    def __init__(self, name: str, repository: str, timeout: float, output_limit: int,
                 repositories: dict[str, Path] | None = None,
                 registry: RepositoryRegistry | None = None) -> None:
        self.name = name
        self.repository_root = Path(repository).resolve()
        # 单元测试和旧调用仍可传入一个具体仓库；生产工厂传入 Catalog 生成的白名单。
        self.repositories = repositories or {self.repository_root.name: self.repository_root}
        self.registry = registry
        self.timeout = timeout
        self.output_limit = output_limit

    async def execute(self, arguments: dict[str, Any]) -> Any:
        """根据白名单操作构造 git argv，并限制读取路径、引用和结果大小。"""
        identifier = str(arguments.get("repository") or "")
        # 运行 SHA 同时用于远程浅抓取和后续 git show；远程仓库不会读取默认分支
        # 来冒充正在运行的代码。
        requested_commit = str(arguments.get("commit") or arguments.get("head") or "HEAD")
        repository = await self._select_repository(identifier, requested_commit)
        if not (repository / ".git").is_dir():
            raise ToolError(f"不是 Git 仓库: {repository}")
        commit = self._safe_ref(str(arguments.get("commit") or "HEAD"))
        base = self._safe_ref(str(arguments.get("base") or f"{commit}^"))
        head = self._safe_ref(str(arguments.get("head") or commit))
        relative_path = self._safe_path(repository, str(arguments.get("path") or ""), required=self.name.startswith("read_file"))
        start_line, end_line = self._safe_line_range(arguments)
        prefix = ["-C", str(repository)]

        mappings: dict[str, list[str]] = {
            "get_repository": prefix + ["rev-parse", "--show-toplevel"],
            "get_current_commit": prefix + ["rev-parse", "HEAD"],
            "get_commit": prefix + ["show", "--no-patch", "--format=fuller", commit],
            "get_previous_commit": prefix + ["rev-parse", f"{commit}^"],
            "get_commit_diff": prefix + ["diff", "--no-ext-diff", "--unified=40", base, head, "--"],
            "read_file_at_commit": prefix + ["show", f"{commit}:{relative_path}"],
            "search_code": prefix + ["grep", "-n", "--no-color", str(arguments.get("pattern") or ""), commit, "--"],
            "list_changed_files": prefix + ["diff", "--name-status", base, head, "--"],
        }
        if self.name == "read_file":
            # 当前工作树读取不用 shell，且 resolve 后必须仍在仓库根目录内部。
            file_path = (repository / relative_path).resolve()
            if not file_path.is_file() or file_path.stat().st_size > self.output_limit * 4:
                raise ToolError("文件不存在或超过安全大小限制")
            content = self._slice_lines(file_path.read_text(encoding="utf-8"), start_line, end_line)
            return bounded({
                "path": relative_path,
                "start_line": start_line,
                "end_line": end_line,
                "content": content,
            }, self.output_limit)
        if self.name == "search_code":
            pattern = str(arguments.get("pattern") or "")
            if not pattern or len(pattern) > 120 or pattern.startswith("-"):
                raise ToolError("pattern 必须是 1~120 字符且不能以 '-' 开头")
        command = mappings.get(self.name)
        if command is None:
            raise ToolError(f"未审核的 Git 操作: {self.name}")
        raw = await run_fixed_command("git", command, timeout=self.timeout)
        if self.name == "read_file_at_commit":
            raw = self._slice_lines(raw, start_line, end_line)
        return bounded({
            "operation": self.name,
            "repository": identifier or repository.name,
            "repository_url": self.registry.remote_url(identifier) if self.registry else None,
            "commit": commit,
            "path": relative_path or None,
            "start_line": start_line if self.name == "read_file_at_commit" else None,
            "end_line": end_line if self.name == "read_file_at_commit" else None,
            "output": raw.strip(),
        }, self.output_limit)

    def _safe_line_range(self, arguments: dict[str, Any]) -> tuple[int | None, int | None]:
        if not self.name.startswith("read_file"):
            return None, None
        raw_start = arguments.get("start_line")
        raw_end = arguments.get("end_line")
        if raw_start is None and raw_end is None:
            return None, None
        try:
            start = int(raw_start or 1)
            end = int(raw_end or start + 199)
        except (TypeError, ValueError) as exc:
            raise ToolError("start_line/end_line 必须是整数") from exc
        if start < 1 or end < start or end - start > 399:
            raise ToolError("源码行范围必须有效且单次最多读取 400 行")
        return start, end

    @staticmethod
    def _slice_lines(content: str, start_line: int | None, end_line: int | None) -> str:
        if start_line is None:
            return content
        lines = content.splitlines()
        return "\n".join(lines[start_line - 1:end_line])

    async def _select_repository(self, identifier: str, commit: str | None = None) -> Path:
        """仅允许选择 Service Catalog 中登记的仓库，禁止任意绝对路径读取。"""
        if self.registry:
            if not identifier and len(self.registry.local_paths) != 1:
                raise ToolError("多仓库模式必须提供 repository")
            selected = identifier or next(iter(self.registry.local_paths))
            return await self.registry.resolve(selected, commit if commit != "HEAD" else None)
        if identifier:
            repository = self.repositories.get(identifier)
            if repository is None:
                raise ToolError(f"未知或未授权 repository: {identifier}")
            return repository
        if len(self.repositories) == 1:
            return next(iter(self.repositories.values()))
        raise ToolError("多仓库模式必须提供 repository")

    def _safe_ref(self, value: str) -> str:
        """限制 Git ref 字符集，避免把引用解析成命令选项。"""
        if value.startswith("-") or not _REF.fullmatch(value) or ".." in value and value.count("..") > 1:
            raise ToolError(f"非法 Git 引用: {value}")
        return value

    def _safe_path(self, repository: Path, value: str, required: bool = False) -> str:
        """把路径约束到仓库内，并统一成 Git 使用的正斜线相对路径。"""
        if required and not value:
            raise ToolError(f"{self.name} 必须提供 path")
        if not value:
            return ""
        candidate = (repository / value).resolve()
        try:
            relative = candidate.relative_to(repository)
        except ValueError as exc:
            raise ToolError("path 不能逃逸仓库根目录") from exc
        return relative.as_posix()


def register_git_tools(
    mcp: FastMCP,
    repository: str,
    catalog_path: str,
    timeout: float,
    output_limit: int,
    registry: RepositoryRegistry | None = None,
) -> None:
    """从 Service Catalog 构建白名单，并注册 FastMCP Git 工具。"""
    root = Path(repository).resolve()
    catalog_file = Path(catalog_path).resolve()
    payload = yaml.safe_load(catalog_file.read_text(encoding="utf-8"))
    repositories: dict[str, Path] = {}
    for identifier, metadata in payload.get("services", {}).items():
        candidate = (catalog_file.parent / str(metadata["repository"])).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Catalog repository escapes SRE workspace: {candidate}") from exc
        repositories[identifier] = candidate
    names = [
        "get_repository", "get_current_commit", "get_commit", "get_commit_diff", "read_file",
        "read_file_at_commit", "search_code", "list_changed_files", "get_previous_commit",
    ]
    for operation in names:
        handler = GitReadBackend(operation, repository, timeout, output_limit, repositories, registry)

        def create_tool(current_handler: GitReadBackend):
            async def git_read(
                repository: str | None = None,
                commit: str | None = None,
                base: str | None = None,
                head: str | None = None,
                path: str | None = None,
                pattern: str | None = None,
                start_line: int | None = None,
                end_line: int | None = None,
            ) -> dict[str, Any]:
                """在 Service Catalog 白名单仓库中执行一个 Git 只读操作。"""
                return await current_handler.execute({
                    "repository": repository,
                    "commit": commit,
                    "base": base,
                    "head": head,
                    "path": path,
                    "pattern": pattern,
                    "start_line": start_line,
                    "end_line": end_line,
                })
            return git_read

        mcp.tool(
            name=operation,
            description=f"本地 Git 只读操作 {operation}；优先使用 Pod 正在运行的完整 Git SHA",
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            tags={"git", "readonly"},
        )(create_tool(handler))
