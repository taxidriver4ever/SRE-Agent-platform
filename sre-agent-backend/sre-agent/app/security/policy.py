"""所有 MCP/第三方 Tool 共享的白名单和参数校验入口。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.security.models import ProjectPolicy, TaskSecurityScope, ToolRisk, ToolRule


class ToolPolicyError(ValueError):
    """工具、项目或参数未通过显式安全策略。"""


_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}$")
_SAFE_SELECTOR = re.compile(r"^[a-zA-Z0-9_.=/,!() -]{0,300}$")


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


_STRING = {"type": "string"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
_MINUTES = {"type": "integer", "minimum": 1, "maximum": 1440}
_REPOSITORY = {"repository": _STRING}


def _rules() -> dict[str, ToolRule]:
    schemas: dict[str, tuple[str, dict[str, Any]]] = {
        "list_namespaces": ("kubernetes", _object({})),
        "list_deployments": ("kubernetes", _object({})),
        "list_pods": ("kubernetes", _object({"label_selector": _STRING})),
        **{
            name: ("kubernetes", _object({"name": _STRING}, ["name"]))
            for name in ("get_pod", "get_pod_events", "get_restart_count", "get_deployment", "get_container_image")
        },
        "query_metrics": ("prometheus", _object({"query": _STRING, "time_range_minutes": _MINUTES}, ["query"])),
        "get_service_health": ("prometheus", _object({"service": _STRING, "time_range_minutes": _MINUTES}, ["service"])),
        "query_logs": ("loki", _object({"service": _STRING, "level": _STRING, "keyword": _STRING, "time_range_minutes": _MINUTES, "limit": _LIMIT})),
        "query_trace": ("tempo", _object({"service": _STRING, "trace_id": _STRING, "limit": _LIMIT})),
        "query_slow_queries": ("mysql", _object({"time_range_minutes": _MINUTES, "limit": _LIMIT})),
        "query_sql_digest": ("mysql", _object({"limit": _LIMIT})),
        "explain_sql": ("mysql", _object({"sql": _STRING}, ["sql"])),
        "get_repository": ("git", _object(_REPOSITORY, ["repository"])),
        "get_current_commit": ("git", _object(_REPOSITORY, ["repository"])),
        "get_commit": ("git", _object({**_REPOSITORY, "commit": _STRING}, ["repository", "commit"])),
        "get_previous_commit": ("git", _object({**_REPOSITORY, "commit": _STRING}, ["repository", "commit"])),
        "get_commit_diff": ("git", _object({**_REPOSITORY, "base": _STRING, "head": _STRING}, ["repository", "base", "head"])),
        "read_file": ("git", _object({**_REPOSITORY, "path": _STRING, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["repository", "path"])),
        "read_file_at_commit": ("git", _object({**_REPOSITORY, "commit": _STRING, "path": _STRING, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["repository", "commit", "path"])),
        "search_code": ("git", _object({**_REPOSITORY, "commit": _STRING, "pattern": _STRING}, ["repository", "commit", "pattern"])),
        "list_changed_files": ("git", _object({**_REPOSITORY, "base": _STRING, "head": _STRING}, ["repository", "base", "head"])),
        "search_conversation_memory": (
            "internal",
            _object({
                "query": _STRING,
                "item_types": {"type": "array", "items": _STRING, "maxItems": 8},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }),
        ),
        "search_code_state": ("internal", _object({"repository_name": _STRING, "query": _STRING, "kinds": {"type": "array", "items": _STRING, "maxItems": 8}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["repository_name", "query"])),
    }
    return {
        name: ToolRule(name, category, ToolRisk.READ_ONLY, schema)
        for name, (category, schema) in schemas.items()
    }


class ToolPolicy:
    """以项目为边界，只放行登记过的只读工具和参数。"""

    def __init__(self, policy_file: str | Path, repository_paths: dict[str, Path]) -> None:
        self.rules = _rules()
        self.repository_paths = {name: path.resolve() for name, path in repository_paths.items()}
        payload = yaml.safe_load(Path(policy_file).read_text(encoding="utf-8")) or {}
        self.projects: dict[str, ProjectPolicy] = {}
        for project_id, raw in payload.get("projects", {}).items():
            repositories: dict[str, tuple[Path, ...]] = {}
            for repository, allowed in (raw.get("repositories") or {}).items():
                root = self.repository_paths.get(repository)
                if root is None:
                    raise ValueError(f"Tool Policy references unknown repository: {repository}")
                roots: list[Path] = []
                for relative in allowed or ["."]:
                    candidate = (root / str(relative)).resolve()
                    try:
                        candidate.relative_to(root)
                    except ValueError as exc:
                        raise ValueError(f"allowed path escapes repository: {candidate}") from exc
                    roots.append(candidate)
                repositories[repository] = tuple(roots)
            enabled = frozenset(str(name) for name in raw.get("enabled_tools", []))
            unknown = enabled.difference(self.rules)
            if unknown:
                raise ValueError(f"Tool Policy contains unknown tools: {sorted(unknown)}")
            self.projects[str(project_id)] = ProjectPolicy(
                str(project_id), str(raw["namespace"]), repositories, enabled
            )

    def project(self, project_id: str) -> ProjectPolicy:
        try:
            return self.projects[project_id]
        except KeyError as exc:
            raise ToolPolicyError(f"unknown or unauthorized project_id: {project_id}") from exc

    def specifications(self, project_id: str) -> dict[str, dict[str, Any]]:
        project = self.project(project_id)
        return {name: self.rules[name].schema for name in project.enabled_tools}

    def authorize(self, name: str, arguments: dict[str, Any], scope: TaskSecurityScope) -> None:
        project = self.project(scope.project_id)
        rule = self.rules.get(name)
        if rule is None or name not in project.enabled_tools or rule.risk is not ToolRisk.READ_ONLY:
            raise ToolPolicyError(f"tool is not allowed for project '{scope.project_id}': {name}")
        if not isinstance(arguments, dict):
            raise ToolPolicyError("tool arguments must be an object")
        allowed_parameters = set(rule.schema["properties"])
        extra = set(arguments).difference(allowed_parameters)
        if extra:
            raise ToolPolicyError(f"unexpected parameters for {name}: {sorted(extra)}")
        missing = set(rule.schema.get("required", [])).difference(
            key for key, value in arguments.items() if value not in (None, "")
        )
        if missing:
            raise ToolPolicyError(f"missing required parameters for {name}: {sorted(missing)}")
        self._validate_common(name, arguments, project)

    def _validate_common(self, name: str, arguments: dict[str, Any], project: ProjectPolicy) -> None:
        namespace = arguments.get("namespace")
        if namespace is not None and namespace != project.namespace:
            raise ToolPolicyError("namespace is outside the project scope")
        for key in ("name", "service", "repository"):
            value = arguments.get(key)
            if value and not _SAFE_NAME.fullmatch(str(value)):
                raise ToolPolicyError(f"invalid {key}")
        selector = arguments.get("label_selector")
        if selector and not _SAFE_SELECTOR.fullmatch(str(selector)):
            raise ToolPolicyError("invalid label_selector")
        repository = arguments.get("repository") or arguments.get("repository_name")
        if repository:
            if repository not in project.repositories:
                raise ToolPolicyError(f"repository is outside project scope: {repository}")
            path = arguments.get("path")
            if path:
                self._validate_path(project, str(repository), str(path))
        query = arguments.get("query")
        if name == "query_metrics" and (
            not isinstance(query, str) or not query.strip() or len(query) > 2000 or "\n" in query
        ):
            raise ToolPolicyError("PromQL must be a single line with 1-2000 characters")
        if name == "query_trace" and not arguments.get("service") and not arguments.get("trace_id"):
            raise ToolPolicyError("query_trace requires service or trace_id")
        trace_id = arguments.get("trace_id")
        if trace_id and not re.fullmatch(r"[0-9a-fA-F]{16,32}", str(trace_id)):
            raise ToolPolicyError("invalid trace_id")
        keyword = arguments.get("keyword")
        if keyword is not None and len(str(keyword)) > 120:
            raise ToolPolicyError("log keyword exceeds 120 characters")
        for key, maximum in (("limit", 100), ("time_range_minutes", 1440)):
            if arguments.get(key) is not None:
                try:
                    value = int(arguments[key])
                except (TypeError, ValueError) as exc:
                    raise ToolPolicyError(f"{key} must be an integer") from exc
                if value < 1 or value > maximum:
                    raise ToolPolicyError(f"{key} is outside the allowed range")

    def _validate_path(self, project: ProjectPolicy, repository: str, value: str) -> None:
        root = self.repository_paths[repository]
        candidate = (root / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ToolPolicyError("path escapes repository root") from exc
        if not any(_is_within(candidate, allowed) for allowed in project.repositories[repository]):
            raise ToolPolicyError("path is outside project allowed_paths")


def _is_within(candidate: Path, allowed: Path) -> bool:
    try:
        candidate.relative_to(allowed)
        return True
    except ValueError:
        return False
