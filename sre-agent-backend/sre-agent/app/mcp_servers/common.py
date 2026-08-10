"""MCP 工具共享的超时、输出裁剪和固定命令执行能力。"""

import json
from typing import Any

from app.core.process import run_fixed_command


def bounded(value: Any, limit: int) -> dict[str, Any]:
    """返回大小受控的结构化结果。

    工具结果先序列化再按字符上限裁剪，既保留 JSON 结构提示，也明确标注是否
    截断。原始几千行日志不会直接进入 LLM 上下文。
    """
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if len(serialized) <= limit:
        return {"data": value, "truncated": False, "characters": len(serialized)}
    return {
        "data": serialized[:limit],
        "truncated": True,
        "characters": len(serialized),
        "notice": f"结果超过 {limit} 字符，已安全截断",
    }


# 兼容现有 MCP 工具的导入路径；实现已放到 core，仓库解析器无需依赖
# ``app.mcp_servers`` 包初始化，从而避免 factory/registry 循环导入。
__all__ = ["bounded", "run_fixed_command"]
