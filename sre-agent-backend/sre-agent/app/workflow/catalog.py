"""Service Catalog 读取与自然语言服务归一化。"""

from pathlib import Path
from typing import Any

import yaml


class ServiceCatalog:
    """把“订单模块”等业务别名映射到 Kubernetes Service 名。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        payload: dict[str, Any] = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.services: dict[str, dict[str, Any]] = payload.get("services", {})

    def resolve(self, query: str) -> str:
        """按最长别名优先匹配，无法判断时返回 unknown 而不是武断猜测。"""
        lowered = query.lower()
        matches: list[tuple[int, str]] = []
        for service, metadata in self.services.items():
            aliases = [service, *metadata.get("aliases", [])]
            for alias in aliases:
                token = str(alias).lower()
                if token in lowered:
                    matches.append((len(token), service))
        return max(matches, default=(0, "unknown"))[1]
