"""从冻结 Commit 中发现 Python FastAPI 与 Java Spring HTTP 接口。"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.process import run_fixed_command
from app.validation.models import InterfaceChangeType, InterfaceTarget, ProjectType


@dataclass(frozen=True)
class _DiscoveredInterface:
    target: InterfaceTarget
    fingerprint: str


class InterfaceDiscovery:
    """只读取 Git object，不 checkout、不修改用户工作树。"""

    MAX_SOURCE_FILES = 500
    MAX_SOURCE_BYTES = 256_000
    _PYTHON_METHODS = {"get","post","put","patch","delete","options","head"}
    _JAVA_METHODS = {
        "GetMapping":"GET","PostMapping":"POST","PutMapping":"PUT",
        "PatchMapping":"PATCH","DeleteMapping":"DELETE","RequestMapping":"ANY",
    }

    async def compare(self, repository_name: str, repository: Path,
                      base_sha: str, candidate_sha: str,
                      project_type: ProjectType) -> list[InterfaceTarget]:
        base = await self._discover(repository_name,repository,base_sha,project_type)
        candidate = await self._discover(repository_name,repository,candidate_sha,project_type)
        output: list[InterfaceTarget] = []
        for key in sorted(base.keys() | candidate.keys()):
            left, right = base.get(key), candidate.get(key)
            if right is None:
                target = left.target.model_copy(update={
                    "change_type":InterfaceChangeType.REMOVED,
                    "base_exists":True,"candidate_exists":False,
                })
            elif left is None:
                target = right.target.model_copy(update={
                    "change_type":InterfaceChangeType.ADDED,
                    "base_exists":False,"candidate_exists":True,
                })
            else:
                change = (InterfaceChangeType.MODIFIED
                          if left.fingerprint != right.fingerprint
                          else InterfaceChangeType.UNCHANGED)
                target = right.target.model_copy(update={
                    "change_type":change,"base_exists":True,"candidate_exists":True,
                })
            output.append(target)
        rank = {InterfaceChangeType.ADDED:0,InterfaceChangeType.MODIFIED:1,
                InterfaceChangeType.REMOVED:2,InterfaceChangeType.UNCHANGED:3}
        return sorted(output,key=lambda item:(rank[item.change_type],item.module_name,
                                               item.route_path,item.interface_name))

    async def source(self, repository: Path, sha: str, target: InterfaceTarget) -> str:
        if not target.source_file or not self._safe_path(target.source_file):
            return ""
        prefix = await self._prefix(repository)
        object_path = f"{prefix}/{target.source_file}" if prefix else target.source_file
        return (await run_fixed_command(
            "git",["-C",str(repository),"show",f"{sha}:{object_path}"],timeout=30,
        ))[: self.MAX_SOURCE_BYTES]

    async def _discover(self, repository_name: str, repository: Path, sha: str,
                        project_type: ProjectType) -> dict[str,_DiscoveredInterface]:
        prefix = await self._prefix(repository)
        treeish = f"{sha}:{prefix}" if prefix else sha
        tree = await run_fixed_command(
            "git",["-C",str(repository),"ls-tree","-r","--name-only",treeish],timeout=30,
        )
        if project_type is ProjectType.PYTHON:
            paths = [p for p in tree.splitlines()
                     if p.endswith(".py") and not p.startswith("tests/")][:self.MAX_SOURCE_FILES]
        elif project_type is ProjectType.MAVEN:
            paths = [p for p in tree.splitlines()
                     if p.startswith("src/main/java/") and p.endswith(".java")][:self.MAX_SOURCE_FILES]
        else:
            return {}
        output: dict[str,_DiscoveredInterface] = {}
        for path in paths:
            if not self._safe_path(path):
                continue
            object_path = f"{prefix}/{path}" if prefix else path
            source = (await run_fixed_command(
                "git",["-C",str(repository),"show",f"{sha}:{object_path}"],timeout=20,
            ))[:self.MAX_SOURCE_BYTES]
            items = (self._python(repository_name,path,source)
                     if project_type is ProjectType.PYTHON
                     else self._java(repository_name,path,source))
            output.update({item.target.id:item for item in items})
        return output

    def _python(self, repository: str, path: str,
                source: str) -> list[_DiscoveredInterface]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        lines = source.splitlines()
        module = self._module_name(path)
        router_prefixes: dict[str,str] = {}
        for node in tree.body:
            if not isinstance(node,(ast.Assign,ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value,ast.Call):
                continue
            function_name = (value.func.id if isinstance(value.func,ast.Name)
                             else value.func.attr if isinstance(value.func,ast.Attribute) else "")
            if function_name != "APIRouter":
                continue
            prefix = ""
            for keyword in value.keywords:
                if keyword.arg == "prefix" and isinstance(keyword.value,ast.Constant):
                    prefix = str(keyword.value.value)
            targets = node.targets if isinstance(node,ast.Assign) else [node.target]
            for assigned in targets:
                if isinstance(assigned,ast.Name):
                    router_prefixes[assigned.id] = prefix
        output: list[_DiscoveredInterface] = []
        for node in ast.walk(tree):
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator,ast.Call) or not isinstance(decorator.func,ast.Attribute):
                    continue
                method = decorator.func.attr.lower()
                if method not in self._PYTHON_METHODS:
                    continue
                route = ""
                if decorator.args and isinstance(decorator.args[0],ast.Constant):
                    route = str(decorator.args[0].value)
                owner = decorator.func.value.id if isinstance(decorator.func.value,ast.Name) else ""
                route = self._join_route(router_prefixes.get(owner,""),route)
                identity = self._identity(repository,path,node.name,method.upper(),route)
                end = getattr(node,"end_lineno",node.lineno)
                snippet = "\n".join(lines[max(0,node.lineno-1):end])
                output.append(_DiscoveredInterface(
                    InterfaceTarget(id=identity,module_name=module,module_path=str(PurePosixPath(path).parent),
                        interface_name=node.name,http_method=method.upper(),route_path=route,
                        source_file=path,symbol=node.name),
                    hashlib.sha256(snippet.encode()).hexdigest(),
                ))
        return output

    def _java(self, repository: str, path: str,
              source: str) -> list[_DiscoveredInterface]:
        module = self._module_name(path)
        class_match = re.search(r'\bclass\s+[A-Za-z_$][\w$]*',source)
        class_offset = class_match.start() if class_match else 0
        base_matches = list(re.finditer(
            r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?"([^"]*)"',
            source[:class_offset],
        ))
        base_match = base_matches[-1] if base_matches else None
        base_route = base_match.group(1) if base_match else ""
        annotation = re.compile(
            r'@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)'
            r'\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?"([^"]*)"[^)]*\))?',re.MULTILINE,
        )
        output: list[_DiscoveredInterface] = []
        matches = [item for item in annotation.finditer(source)
                   if not base_match or item.start() != base_match.start()]
        for index,item in enumerate(matches):
            tail = source[item.end():item.end()+800]
            method_match = re.search(
                r'(?:public|protected|private)?\s*(?:static\s+)?[\w<>,.?\[\]\s]+'
                r'\s+([A-Za-z_$][\w$]*)\s*\(',tail,
            )
            if method_match is None:
                continue
            symbol = method_match.group(1)
            http_method = self._JAVA_METHODS[item.group(1)]
            if item.group(1) == "RequestMapping":
                request_method = re.search(
                    r'\bmethod\s*=\s*RequestMethod\.([A-Z]+)',item.group(0),
                )
                if request_method:
                    http_method = request_method.group(1)
            route = self._join_route(base_route,item.group(2) or "")
            identity = self._identity(repository,path,symbol,http_method,route)
            end = matches[index+1].start() if index+1 < len(matches) else min(len(source),item.start()+8000)
            snippet = source[item.start():end]
            output.append(_DiscoveredInterface(
                InterfaceTarget(id=identity,module_name=module,module_path=str(PurePosixPath(path).parent),
                    interface_name=symbol,http_method=http_method,route_path=route,
                    source_file=path,symbol=symbol),
                hashlib.sha256(snippet.encode()).hexdigest(),
            ))
        return output

    async def _prefix(self, repository: Path) -> str:
        return (await run_fixed_command(
            "git",["-C",str(repository),"rev-parse","--show-prefix"],timeout=10,
        )).strip().rstrip("/")

    @staticmethod
    def _module_name(path: str) -> str:
        value = str(PurePosixPath(path).with_suffix(""))
        return value.replace("/",".")

    @staticmethod
    def _identity(repository: str, path: str, symbol: str,
                  method: str, route: str) -> str:
        return hashlib.sha256(
            f"{repository}:{path}:{symbol}:{method}:{route}".encode(),
        ).hexdigest()

    @staticmethod
    def _join_route(base: str, route: str) -> str:
        combined = "/".join(part.strip("/") for part in (base,route) if part)
        return f"/{combined}" if combined else ""

    @staticmethod
    def _safe_path(path: str) -> bool:
        value = PurePosixPath(path)
        return not value.is_absolute() and ".." not in value.parts and "\\" not in path
