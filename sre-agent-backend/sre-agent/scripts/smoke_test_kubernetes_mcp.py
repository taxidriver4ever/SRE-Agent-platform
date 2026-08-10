"""真实连接第三方 Kubernetes MCP Server 的最小只读冒烟脚本。"""

import asyncio
import json

from app.mcp_clients import KubernetesMCPAdapter


async def main() -> None:
    """列出 order-service Pod，并输出不含敏感字段的验证摘要。"""
    adapter = KubernetesMCPAdapter("sre-lab")
    try:
        result = await adapter.call("list_pods", {"label_selector": "app=order-service"})
        data = result.get("data") or {}
        print(json.dumps({
            "source": result.get("source"),
            "pod_count": len(data.get("items", [])),
            "kind": data.get("kind"),
        }, ensure_ascii=False))
    finally:
        # 即使调用失败也关闭 npx stdio 子进程，避免测试遗留后台进程。
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
