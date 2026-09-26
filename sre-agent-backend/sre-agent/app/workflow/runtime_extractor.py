"""从 Kubernetes/Tool Result 提取运行对象元数据。"""

import json
import re
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
        value = str(annotations.get("sre.agent/git-sha") or annotations.get("sre-agent/source-sha") or "")
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
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
        metadata_by_pod: dict[str, dict] = {}
        for pod in payload["data"]["items"]:
            pod_name = str(pod["metadata"]["name"])
            annotations = pod.get("metadata", {}).get("annotations", {})
            image = str(pod["spec"]["containers"][0]["image"])
            # Digest-only and SHA@digest images retain their source identity in metadata.
            version = extract_git_sha({"data": {"annotations": annotations}}) or image.split("@", 1)[0].rsplit(":", 1)[-1]
            if re.fullmatch(r"[0-9a-f]{40}", version):
                candidates.append((pod_name, version))
                metadata_by_pod[pod_name] = annotations
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
        # Bind source metadata from the selected Pod, not the last Pod in the list.
        annotations = metadata_by_pod[state.pod_name]
        previous = str(annotations.get("sre.agent/previous-git-sha") or "")
        state.previous_runtime_commit = previous if re.fullmatch(r"[0-9a-f]{40}", previous) else None
        if annotations.get("sre.agent/repository"):
            state.repository = str(annotations["sre.agent/repository"])
        if annotations.get("sre.agent/source-path"):
            state.source_code_location = str(annotations["sre.agent/source-path"])
        if annotations.get("sre.agent/language"):
            state.language = str(annotations["sre.agent/language"])
        repository_url = str(annotations.get("sre.agent/repository-url") or "")
        if repository_url and state.repository and repository_registry:
            state.repository_url = repository_registry.bind(state.repository, repository_url)
    except (IndexError, KeyError, TypeError):
        return


def extract_trace_id(payload: Any) -> str | None:
    """Read a normalized log's actual trace ID (native or W3C)."""
    from app.mcp_servers.observability.adapters import TRACE_ID
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for record in data.get("logs", []):
        identifier = str(record.get("trace_id") or "")
        if TRACE_ID.fullmatch(identifier):
            return identifier
    return None
