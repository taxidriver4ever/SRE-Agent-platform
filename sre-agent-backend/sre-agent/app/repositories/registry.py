"""把 Kubernetes 工作负载注解映射为可审计的远程 Git 仓库。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from fastmcp.exceptions import ToolError

from app.core.process import run_fixed_command


class RepositoryRegistry:
    """维护 Service → remote URL/local mirror，并按运行 commit 准备只读仓库。

    远程 URL 只能来自 Kubernetes 注解或 Service Catalog，且必须命中显式主机
    白名单。即使集群中的注解被污染，也不能诱导 Agent 访问内网地址、带凭据
    URL 或任意本机路径。
    """

    def __init__(
        self,
        repository_root: str,
        catalog_path: str,
        cache_path: str,
        allowed_hosts: tuple[str, ...],
        timeout: float,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.cache_path = Path(cache_path).resolve()
        self.allowed_hosts = {host.lower() for host in allowed_hosts if host}
        self.timeout = timeout
        catalog_file = Path(catalog_path).resolve()
        payload = yaml.safe_load(catalog_file.read_text(encoding="utf-8")) or {}
        self.local_paths: dict[str, Path] = {}
        self.remote_urls: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        for service, metadata in payload.get("services", {}).items():
            local = (catalog_file.parent / str(metadata["repository"])).resolve()
            try:
                local.relative_to(self.repository_root)
            except ValueError as exc:
                raise ValueError(f"Catalog repository escapes SRE workspace: {local}") from exc
            self.local_paths[str(service)] = local
            if metadata.get("repository_url"):
                self.bind(str(service), str(metadata["repository_url"]))

    def bind(self, service: str, repository_url: str) -> str:
        """校验并保存由 K8s 发现的远程仓库 URL，返回去凭据后的规范 URL。"""
        if service not in self.local_paths:
            raise ToolError(f"Kubernetes 引用了 Catalog 外的服务仓库: {service}")
        parts = urlsplit(repository_url.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ToolError("repository-url 必须是带主机名的 HTTPS Git URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ToolError("repository-url 不能携带账号、密码、query 或 fragment")
        if parts.hostname.lower() not in self.allowed_hosts:
            raise ToolError(f"repository-url 主机不在白名单: {parts.hostname}")
        # 丢弃无意义的尾斜线并统一主机大小写，确保缓存键和证据引用稳定。
        normalized = urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        self.remote_urls[service] = normalized
        return normalized

    def remote_url(self, service: str | None) -> str | None:
        """取得当前运行时绑定的远程 URL；未绑定时明确返回 None。"""
        return self.remote_urls.get(service or "")

    async def resolve(self, service: str, commit: str | None = None) -> Path:
        """优先准备远程只读缓存；远程未绑定时使用现有本地镜像仓库。"""
        if service not in self.local_paths:
            raise ToolError(f"未知或未授权 repository: {service}")
        repository_url = self.remote_urls.get(service)
        if not repository_url:
            return self.local_paths[service]

        digest = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:16]
        target = self.cache_path / f"{service}-{digest}"
        lock = self._locks.setdefault(service, asyncio.Lock())
        async with lock:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            if not (target / ".git").is_dir():
                await run_fixed_command(
                    "git",
                    ["clone", "--filter=blob:none", "--no-checkout", repository_url, str(target)],
                    timeout=max(self.timeout, 60),
                )
            if commit:
                # 只抓取正在运行的精确 SHA，既控制磁盘体积，也避免用远端默认分支
                # 代替运行版本而产生 Source Reference 漂移。
                await run_fixed_command(
                    "git",
                    # Keep the immediate parent for first-release regression diffs.
                    ["-C", str(target), "fetch", "--depth=2", "origin", commit],
                    timeout=max(self.timeout, 60),
                )
        return target

    def repositories(self) -> list[str]:
        """返回 Service Catalog 中已经授权的仓库名称。"""
        return sorted(self.local_paths)

    async def list_branches(self, service: str) -> list[str]:
        """只列出授权仓库真实存在的本地或 origin 分支。"""
        repository = await self.resolve(service)
        if service in self.remote_urls:
            # 对显式白名单远程仓库刷新 heads，确保新建 Validation 解析的是
            # 请求时最新的远端分支，而不是旧缓存中的 origin/*。本地实验仓库
            # 没有可信远程 URL 时保持纯本地读取，不猜测或访问任意 origin。
            lock = self._locks.setdefault(service, asyncio.Lock())
            async with lock:
                await run_fixed_command(
                    "git",
                    ["-C", str(repository), "fetch", "--prune", "origin",
                     "+refs/heads/*:refs/remotes/origin/*"],
                    timeout=max(self.timeout, 60),
                )
        output = await run_fixed_command(
            "git",
            ["-C", str(repository), "for-each-ref", "--format=%(refname)",
             "refs/heads", "refs/remotes/origin"],
            timeout=self.timeout,
        )
        branches: set[str] = set()
        for line in output.splitlines():
            ref = line.strip()
            if ref.startswith("refs/heads/"):
                branches.add(ref.removeprefix("refs/heads/"))
            elif ref.startswith("refs/remotes/origin/") and not ref.endswith("/HEAD"):
                branches.add(ref.removeprefix("refs/remotes/origin/"))
        return sorted(branch for branch in branches if self._safe_branch(branch))

    async def resolve_ref(self, service: str, branch: str) -> tuple[Path, str]:
        """把下拉框中的授权 Branch 冻结为不可变的完整 Commit SHA。"""
        if branch not in await self.list_branches(service):
            raise ToolError("branch is not present in the authorized repository")
        repository = await self.resolve(service)
        candidates = (
            [f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"]
            if service in self.remote_urls
            else [f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"]
        )
        for ref in candidates:
            try:
                output = await run_fixed_command(
                    "git", ["-C", str(repository), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                    timeout=self.timeout,
                )
            except ToolError:
                continue
            commit = output.strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", commit):
                return repository, commit
        raise ToolError("branch did not resolve to a full commit SHA")

    @staticmethod
    def _safe_branch(branch: str) -> bool:
        return bool(
            branch and len(branch) <= 240 and not branch.startswith(("-", "."))
            and ".." not in branch and "@{" not in branch and "\\" not in branch
            and not branch.endswith((".", "/", ".lock")) and "//" not in branch
            and all(character not in branch for character in " ~^:?*[")
            and all(32 <= ord(character) < 127 for character in branch)
        )
