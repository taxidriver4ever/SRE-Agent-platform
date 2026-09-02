"""从 Kubernetes/Tool Result 提取运行对象元数据。"""

import json
from typing import Any

from app.repositories import RepositoryRegistry
from app.workflow.models import DiagnosisState


def find_pod_name(payload: Any, service: str) -> str | None:
    try:
        items = payload["data"]["items"]
        return next(item["metadata"]["name"] for item in items if item["metadata"]["name"].startswith(service))
    except (KeyError, TypeError, StopIteration):
        return None


def extract_git_sha(payload: Any) -> str | None:
    try:
        annotations = payload["data"]["annotations"]
        value = str(annotations.get("sre.agent/git-sha", ""))
        return value if len(value) == 40 else None
    except (KeyError, TypeError):
        return None


def extract_pod_runtime(
    state: DiagnosisState,
    payload: Any,
    repository_registry: RepositoryRegistry | None = None,
) -> None:
    """比较同一 Service 的镜像版本，并选择少数版本作为疑似异常实例。"""
    try:
        candidates: list[tuple[str, str]] = []
        for pod in payload["data"]["items"]:
            pod_name = str(pod["metadata"]["name"])
            annotations = pod.get("metadata", {}).get("annotations", {})
            repository = str(annotations.get("sre.agent/repository") or "")
            repository_url = str(annotations.get("sre.agent/repository-url") or "")
            if repository:
                state.repository = repository
            if annotations.get("sre.agent/source-path"):
                state.source_code_location = str(annotations["sre.agent/source-path"])
            if annotations.get("sre.agent/language"):
                state.language = str(annotations["sre.agent/language"])
            if repository_url and state.repository and repository_registry:
                state.repository_url = repository_registry.bind(state.repository, repository_url)
            image = str(pod["spec"]["containers"][0]["image"])
            version = image.rsplit(":", 1)[-1]
            if len(version) == 40:
                candidates.append((pod_name, version))
        if not candidates:
            return
        state.pod_versions = dict(candidates)
        counts: dict[str, int] = {}
        for _, version in candidates:
            counts[version] = counts.get(version, 0) + 1
        state.mixed_versions = len(counts) > 1
        selected = min(counts, key=counts.get) if state.mixed_versions else candidates[0][1]
        state.pod_name = next(pod for pod, version in candidates if version == selected)
        state.runtime_commit = selected
    except (IndexError, KeyError, TypeError):
        return


def extract_trace_id(payload: Any) -> str | None:
    """从 Loki bounded 结果中提取最近一条合法 trace_id。"""
    try:
        streams = payload["data"]["result"]["result"]
        for stream in streams:
            for value in reversed(stream.get("values", [])):
                if not isinstance(value, list) or len(value) < 2:
                    continue
                record = json.loads(value[1])
                trace_id = str(record.get("trace_id", ""))
                if len(trace_id) in {16, 32} and all(char in "0123456789abcdefABCDEF" for char in trace_id):
                    return trace_id
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return None
