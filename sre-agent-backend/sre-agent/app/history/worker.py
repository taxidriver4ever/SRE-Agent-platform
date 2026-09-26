"""MySQL retry queue -> optional indexes, outside Diagnosis completion transactions."""
import asyncio
import logging
import httpx

from app.history.models import HistoryItem
from app.history.repository import HistoryRepository
from app.history.elasticsearch import HistoryIndex

logger = logging.getLogger(__name__)


class HistorySyncWorker:
    def __init__(self, source: HistoryRepository, index: HistoryIndex, *, interval: float = 30,
                 batch_size: int = 20, logstash_url: str = "", logstash_token: str = "") -> None:
        self.source, self.index = source, index
        self.interval = max(1, interval)
        self.batch_size = max(1, min(batch_size, 100))
        self.logstash_url = logstash_url
        self.logstash_token = logstash_token
        self.task: asyncio.Task | None = None
        self.stopping = asyncio.Event()

    async def run_once(self) -> dict[str, int]:
        captured = await asyncio.to_thread(self.source.capture_completed, self.batch_size)
        indexed = 0
        for row in await asyncio.to_thread(self.source.pending, self.batch_size):
            try:
                await self.index.index(HistoryItem.model_validate_json(row["document_json"]))
                await asyncio.to_thread(self.source.acknowledge, row["history_id"])
                indexed += 1
            except Exception as exc:
                await asyncio.to_thread(self.source.retry, row["history_id"], row["retry_count"], type(exc).__name__)
                logger.warning("History indexing delayed: reason=%s", type(exc).__name__)
        delivered = 0
        if self.logstash_url:
            headers = {"Authorization": f"Bearer {self.logstash_token}"} if self.logstash_token else {}
            async with httpx.AsyncClient(timeout=2, headers=headers) as client:
                for event in await asyncio.to_thread(self.source.pending_events, self.batch_size):
                    try:
                        response = await client.post(self.logstash_url, json=event)
                        response.raise_for_status()
                        await asyncio.to_thread(self.source.acknowledge_event, int(event["event_id"]))
                        delivered += 1
                    except Exception as exc:
                        logger.warning("Event search projection delayed: reason=%s", type(exc).__name__)
                        break
        return {"captured": captured, "indexed": indexed, "events": delivered}

    def start(self) -> None:
        if self.task is None:
            self.stopping.clear()
            self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("History sync delayed: reason=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self.stopping.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
