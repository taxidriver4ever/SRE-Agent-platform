"""Optional, bounded retrieval and evidence-shaped historical context."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from app.history.elasticsearch import HistoryIndex
from app.history.models import HistoryQuery
from app.history.repository import HistoryRepository
from app.history.sanitize import sanitize
from app.workflow.models import Evidence

logger = logging.getLogger(__name__)


class HistoryContextBuilder:
    @staticmethod
    def build(items, budget: int = 4000) -> str:
        references = []
        for item in items:
            evidence = Evidence(
                source="History", source_type="HISTORY", evidence_type="historical_case",
                tool_name="history_retrieval", evidence_id=f"history:{item.history_id}",
                service_name=item.service, timestamp=item.timestamp, title="Historical diagnosis (reference only)",
                detail=sanitize(item.summary[:400]), supports_conclusion=False, direct_evidence=False,
                raw_reference=f"/api/diagnoses/{item.task_id}",
                structured_data=sanitize({"task_id": item.task_id, "category": item.category,
                    "symptom": item.symptom[:200], "root_cause": item.root_cause[:400],
                    "conclusion_status": item.conclusion_status, "evidence_summary": item.evidence_summary[:3]}),
            )
            row = evidence.model_dump(mode="json", exclude_defaults=True)
            candidate = json.dumps([*references, row], ensure_ascii=False)
            if len(candidate) > budget:
                break
            references.append(row)
        return json.dumps(references, ensure_ascii=False)


class HistoryRetrievalService:
    def __init__(self, source: HistoryRepository, index: HistoryIndex, *, timeout: float = 2,
                 top_k: int = 5, lookback_days: int = 365, budget: int = 4000) -> None:
        self.source, self.index = source, index
        self.timeout = max(.1, min(timeout, 10))
        self.top_k = max(1, min(top_k, 10))
        self.lookback_days = max(1, min(lookback_days, 3650))
        self.budget = max(512, min(budget, 6000))

    async def retrieve(self, user_id: str, project_id: str, query: HistoryQuery) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                ids = await self.index.search(user_id, project_id, query)
                items = await asyncio.to_thread(self.source.hydrate, ids, user_id, project_id, query)
                return HistoryContextBuilder.build(items, self.budget)
        except Exception as exc:
            # Do not log response bodies, credentials or incident content.
            logger.warning("History Retrieval skipped: reason=%s", type(exc).__name__)
            return "[]"

    async def for_incident(self, user_id: str, project_id: str, service: str,
                           symptom: str, task_id: str | None = None) -> str:
        if not user_id or not project_id or service == "unknown":
            return "[]"
        now = datetime.now(timezone.utc)
        return await self.retrieve(user_id, project_id, HistoryQuery(
            service=service, text=symptom[:600], start_time=now - timedelta(days=self.lookback_days),
            end_time=now, limit=self.top_k, exclude_task_id=task_id))
