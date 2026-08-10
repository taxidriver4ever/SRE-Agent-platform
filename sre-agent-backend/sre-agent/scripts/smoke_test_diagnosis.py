"""对真实 Kind/Prometheus/Loki/Tempo/MySQL/Git 执行一次无 LLM 的诊断冒烟。"""

import asyncio
import json

from app.context import ActiveContextCompactor, EvidenceStore
from app.core.config import get_settings
from app.mcp_clients import FastMCPToolClient, KubernetesMCPAdapter
from app.mcp_servers import build_fastmcp_server
from app.repositories import RepositoryRegistry
from app.workflow import DiagnosisWorkflow


async def main() -> None:
    """运行固定延迟问题并输出证据链摘要，不打印数据库密码或完整日志。"""
    settings = get_settings()
    registry = RepositoryRegistry(
        settings.repository_path,
        settings.service_catalog_path,
        settings.repository_cache_path,
        settings.repository_allowed_hosts,
        settings.tool_timeout_seconds,
    )
    server = build_fastmcp_server(settings, registry)
    tools = FastMCPToolClient(server, KubernetesMCPAdapter(settings.kubernetes_namespace))
    workflow = DiagnosisWorkflow(
        tools,
        settings.service_catalog_path,
        max_steps=12,
        repository_registry=registry,
        evidence_store=EvidenceStore(),
        compactor=ActiveContextCompactor(settings.active_context_character_budget),
        kubernetes_namespace=settings.kubernetes_namespace,
    )
    try:
        report = await workflow.run("为什么订单接口有时候很快，有时候特别慢？")
        print(json.dumps({
            "service": report.service,
            "affected_pod": report.affected_pod,
            "git_sha": report.git_sha,
            "evidence_count": len(report.evidence),
            "sources": sorted({item.source for item in report.evidence}),
            "source_reference_count": sum(len(item.source_references) for item in report.evidence),
            "stored_evidence": report.context_compaction["stored_evidence"],
        }, ensure_ascii=False))
    finally:
        await tools.close()


if __name__ == "__main__":
    asyncio.run(main())
