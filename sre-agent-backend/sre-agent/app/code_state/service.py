"""首次有界仓库扫描与基于 Git Diff 的 Code State 增量更新。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.code_state.models import CodeComponent, CodeReference, CodeRolePatch
from app.code_state.repository import CodeStateRepository
from app.core.process import run_fixed_command
from app.llm.base import LLM, LLMMessage
from app.llm.structured_output import validate_structured_output
from app.repositories import RepositoryRegistry
from fastmcp.exceptions import ToolError


_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_ENTRY_NAME = re.compile(
    r"(Controller|Service|ServiceImpl|Repository|Mapper|Client|Handler|Router|Routes|UseCase)\.(java|kt|py|js|ts|tsx)$",
    re.IGNORECASE,
)
_MANIFESTS = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json"}
_CONFIG_SUFFIXES = (".yml", ".yaml", ".properties", ".toml")


class CodeStateService:
    """源码始终留在 Git；本服务只生成短导航 State 和 Git Reference。"""

    def __init__(
        self,
        repository: CodeStateRepository,
        registry: RepositoryRegistry,
        llm: LLM | None = None,
        *,
        timeout: float = 30,
        max_files: int = 120,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.llm = llm
        self.timeout = timeout
        self.max_files = max(20, min(max_files, 200))
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(self, repository: str, commit_sha: str) -> str:
        """首次全量导航扫描；后续只处理 old..new Diff 触及的文件。"""
        if not _COMMIT.fullmatch(commit_sha):
            raise ValueError("Code State requires a full 40-character commit SHA")
        lock = self._locks.setdefault(repository, asyncio.Lock())
        async with lock:
            old_commit = self.repository.current_commit(repository)
            if old_commit == commit_sha:
                return "unchanged"
            repo_path = await self.registry.resolve(repository, commit_sha)
            if old_commit is None:
                await self._initial_scan(repository, repo_path, commit_sha)
                return "initialized"
            try:
                await self._incremental_update(repository, repo_path, old_commit, commit_sha)
                return "updated"
            except ToolError:
                # 浅缓存若已清理旧 Commit，无法计算 old..new；安全回退为新 Commit
                # 的有界导航扫描，仍不会把仓库全文交给模型。
                await self._initial_scan(repository, repo_path, commit_sha)
                return "reinitialized"

    async def _initial_scan(self, repository: str, repo_path: Path, commit_sha: str) -> None:
        paths = await self._tree(repo_path, commit_sha)
        selected = self._select_initial_paths(paths)
        contents = await self._read_selected(repo_path, commit_sha, selected)
        components = self._build_components(repository, commit_sha, contents)
        components = await self._enrich_roles(components, "initial repository navigation")
        self.repository.replace_repository(
            repository,
            self.registry.remote_url(repository),
            commit_sha,
            self._directory_summary(paths),
            components,
        )

    async def _incremental_update(
        self,
        repository: str,
        repo_path: Path,
        old_commit: str,
        new_commit: str,
    ) -> None:
        status_text = await run_fixed_command(
            "git",
            ["-C", str(repo_path), "diff", "--name-status", "-M", old_commit, new_commit, "--"],
            timeout=self.timeout,
        )
        removed: set[str] = set()
        changed: set[str] = set()
        for line in status_text.splitlines():
            fields = line.split("\t")
            if not fields:
                continue
            status = fields[0]
            if status.startswith("R") and len(fields) >= 3:
                removed.add(fields[1])
                changed.add(fields[2])
            elif status == "D" and len(fields) >= 2:
                removed.add(fields[1])
            elif status[:1] in {"A", "M", "C", "T"} and len(fields) >= 2:
                changed.add(fields[-1])
        relevant_changed = {path for path in changed if self._is_navigation_file(path)}
        contents = await self._read_selected(repo_path, new_commit, sorted(relevant_changed))
        components = self._build_components(repository, new_commit, contents)
        diff_text = await run_fixed_command(
            "git",
            ["-C", str(repo_path), "diff", "--no-ext-diff", "--unified=8", old_commit, new_commit, "--", *sorted(changed)],
            timeout=self.timeout,
        ) if changed else ""
        components = await self._enrich_roles(components, diff_text[:16000])
        paths = await self._tree(repo_path, new_commit)
        self.repository.apply_incremental(
            repository,
            self.registry.remote_url(repository),
            new_commit,
            self._directory_summary(paths),
            removed,
            components,
        )

    async def _tree(self, repo_path: Path, commit_sha: str) -> list[str]:
        output = await run_fixed_command(
            "git", ["-C", str(repo_path), "ls-tree", "-r", "--name-only", commit_sha],
            timeout=self.timeout,
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _select_initial_paths(self, paths: list[str]) -> list[str]:
        manifests = [path for path in paths if Path(path).name in _MANIFESTS]
        configs = [path for path in paths if self._is_config(path)][:40]
        entries = [path for path in paths if _ENTRY_NAME.search(Path(path).name)][:100]
        return list(dict.fromkeys([*manifests, *configs, *entries]))[:self.max_files]

    async def _read_selected(
        self,
        repo_path: Path,
        commit_sha: str,
        paths: list[str],
    ) -> dict[str, str]:
        contents: dict[str, str] = {}
        for path in paths[:self.max_files]:
            try:
                text = await run_fixed_command(
                    "git", ["-C", str(repo_path), "show", f"{commit_sha}:{path}"],
                    timeout=self.timeout,
                )
            except Exception:
                continue
            # 解析器只需要签名和配置导航；单文件不保留或发送无限正文。
            contents[path] = text[:40000]
        return contents

    def _build_components(
        self,
        repository: str,
        commit_sha: str,
        contents: dict[str, str],
    ) -> list[CodeComponent]:
        drafts: list[tuple[str, str, str, int | None, int | None, str]] = []
        for path, content in contents.items():
            kind = self._kind(path)
            matches = self._symbols(path, content)
            if not matches:
                matches = [(path, 1, min(max(1, content.count("\n") + 1), 200))]
            for symbol, start, end in matches[:30]:
                drafts.append((path, kind, symbol, start, end, self._role(path, symbol, kind)))
        symbols = [item[2] for item in drafts]
        components: list[CodeComponent] = []
        for path, kind, symbol, start, end, role in drafts:
            content = contents[path]
            relationships = [
                candidate for candidate in symbols
                if candidate != symbol and candidate.split("#", 1)[0].split(".")[-1] in content
            ][:12]
            components.append(CodeComponent(
                module=repository,
                kind=kind,
                symbol=symbol,
                path=path,
                role=role,
                relationships=relationships,
                reference=CodeReference(
                    repo=repository,
                    commit_sha=commit_sha,
                    path=path,
                    symbol=symbol.split("#")[-1],
                    start_line=start,
                    end_line=end,
                ),
            ))
        return components

    async def _enrich_roles(
        self,
        components: list[CodeComponent],
        change_summary: str,
    ) -> list[CodeComponent]:
        """LLM 只看 Diff 和签名导航；路径、行号和 Commit 仍由 Git 决定。"""
        if self.llm is None or not components:
            return components
        navigation = [{
            "path": item.path,
            "symbol": item.symbol,
            "kind": item.kind,
            "current_role": item.role,
        } for item in components[:40]]
        try:
            response = await self.llm.complete([
                LLMMessage(
                    "system",
                    "你是 Code State 导航维护器。只根据 Git diff 和符号签名完善短 role/relationships；"
                    "不得输出源码，不得创建输入中不存在的 path 或 symbol。只输出 JSON。/no_think",
                ),
                LLMMessage("user", json.dumps({
                    "change_summary": change_summary[:16000],
                    "navigation": navigation,
                }, ensure_ascii=False)),
            ])
            patch = validate_structured_output(response.content, CodeRolePatch)
        except Exception:
            return components
        allowed = {(item.path, item.symbol) for item in components}
        allowed_symbols = {item.symbol for item in components}
        updates = {
            (item.path, item.symbol): item
            for item in patch.items
            if (item.path, item.symbol) in allowed
        }
        return [
            item.model_copy(update={
                "role": updates[(item.path, item.symbol)].role,
                "relationships": [
                    relation for relation in updates[(item.path, item.symbol)].relationships
                    if relation in allowed_symbols
                ],
            }) if (item.path, item.symbol) in updates else item
            for item in components
        ]

    @staticmethod
    def _symbols(path: str, content: str) -> list[tuple[str, int, int]]:
        lines = content.splitlines()
        class_name = Path(path).stem
        results: list[tuple[str, int, int]] = []
        patterns = []
        if path.endswith((".java", ".kt")):
            patterns = [re.compile(r"\b(?:public|protected|private)?\s*(?:static\s+)?[\w<>,.?\[\]]+\s+(\w+)\s*\(")]
        elif path.endswith(".py"):
            patterns = [re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")]
        elif path.endswith((".js", ".ts", ".tsx")):
            patterns = [re.compile(r"\b(?:async\s+)?(?:function\s+)?(\w+)\s*\([^)]*\)\s*(?:\{|=>)")]
        for index, line in enumerate(lines, start=1):
            for pattern in patterns:
                match = pattern.search(line)
                if match and match.group(1) not in {"if", "for", "while", "switch", "catch"}:
                    results.append((f"{class_name}#{match.group(1)}", index, min(len(lines), index + 40)))
                    break
        results.sort(key=lambda item: item[1])
        return [
            (
                symbol,
                start,
                max(start, results[index + 1][1] - 1) if index + 1 < len(results)
                else min(len(lines), start + 80),
            )
            for index, (symbol, start, _) in enumerate(results)
        ]

    @staticmethod
    def _is_config(path: str) -> bool:
        lower = path.lower()
        return lower.endswith(_CONFIG_SUFFIXES) and any(
            part in lower for part in ("config", "application", "bootstrap", "resources")
        )

    @classmethod
    def _is_navigation_file(cls, path: str) -> bool:
        return Path(path).name in _MANIFESTS or cls._is_config(path) or bool(_ENTRY_NAME.search(Path(path).name))

    @classmethod
    def _kind(cls, path: str) -> str:
        name = Path(path).name.lower()
        if Path(path).name in _MANIFESTS:
            return "manifest"
        if cls._is_config(path):
            return "config"
        for token, kind in (("controller", "controller"), ("service", "service"), ("repository", "repository"), ("mapper", "repository")):
            if token in name:
                return kind
        return "component"

    @staticmethod
    def _role(path: str, symbol: str, kind: str) -> str:
        roles = {
            "manifest": "项目构建与依赖导航",
            "config": "运行配置导航",
            "controller": "请求入口导航",
            "service": "核心业务逻辑导航",
            "repository": "数据访问导航",
            "component": "代码组件导航",
        }
        return f"{roles.get(kind, '模块导航')}：{symbol}"

    @staticmethod
    def _directory_summary(paths: list[str]) -> dict[str, Any]:
        top_levels: dict[str, int] = {}
        for path in paths:
            top = path.split("/", 1)[0]
            top_levels[top] = top_levels.get(top, 0) + 1
        return {
            "file_count": len(paths),
            "top_level": dict(sorted(top_levels.items(), key=lambda item: (-item[1], item[0]))[:30]),
            "manifests": [path for path in paths if Path(path).name in _MANIFESTS][:20],
        }
