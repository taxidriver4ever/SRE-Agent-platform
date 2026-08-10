"""Active Context Compaction：按当前任务主动选择并压缩证据。"""

from __future__ import annotations

import json
from typing import Any


class ActiveContextCompactor:
    """把完整 Evidence Store 转换为受预算约束的活动上下文。

    压缩不是简单截断最近 N 条：优先保留支持结论的证据、不同来源以及源码
    引用，同时为每条证据保留 evidence_id，模型或用户可追溯到原始结果。
    """

    def __init__(self, character_budget: int = 8000, item_budget: int = 12) -> None:
        self.character_budget = max(1000, character_budget)
        self.item_budget = max(2, item_budget)

    def compact_result(self, value: Any, limit: int = 900) -> str:
        """生成单条证据预览；原文已入 Store，因此裁剪不会造成证据丢失。"""
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        return text if len(text) <= limit else f"{text[:limit]}…（完整结果见 Evidence Store）"

    def build_active_context(self, evidence: list[Any]) -> str:
        """以来源多样性和结论相关性排序，产出给 LLM 的紧凑事实块。"""
        ranked = sorted(
            enumerate(evidence),
            key=lambda pair: (
                0 if getattr(pair[1], "supports_conclusion", False) else 1,
                0 if getattr(pair[1], "source_references", []) else 1,
                -pair[0],
            ),
        )
        selected: list[Any] = []
        seen_sources: set[str] = set()
        # 第一轮先保证来源多样性，避免大量同类日志挤掉 Trace/SQL/源码证据。
        for _, item in ranked:
            source = str(getattr(item, "source", "unknown"))
            if source not in seen_sources:
                selected.append(item)
                seen_sources.add(source)
            if len(selected) >= self.item_budget:
                break
        for _, item in ranked:
            if item not in selected:
                selected.append(item)
            if len(selected) >= self.item_budget:
                break

        lines: list[str] = []
        used = 0
        for item in selected:
            refs = ", ".join(ref.uri for ref in getattr(item, "source_references", [])) or "无精确引用"
            line = (
                f"[{item.evidence_id}] [{item.source}] {item.title}\n"
                f"摘要: {item.detail}\n来源: {refs}"
            )
            if used + len(line) > self.character_budget:
                remaining = self.character_budget - used
                if remaining > 120:
                    lines.append(line[:remaining] + "…")
                break
            lines.append(line)
            used += len(line) + 2
        return "\n\n".join(lines)
