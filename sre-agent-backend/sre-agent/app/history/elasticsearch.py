"""Fixed, bounded Elasticsearch documents. No model-supplied DSL or index names."""
import asyncio
import json
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
from app.history.models import HistoryItem, HistoryQuery


class HistoryIndex(Protocol):
    async def index(self, item: HistoryItem) -> None: ...
    async def search(self, user_id: str, project_id: str, query: HistoryQuery) -> list[str]: ...


class HistoryIndexError(RuntimeError):
    def __init__(self, status: int, kind: str) -> None:
        super().__init__(f"history index HTTP {status}")
        self.status, self.kind = status, kind


class ElasticsearchHistoryIndex:
    def __init__(self, url: str, index: str, *, username: str | None = None,
                 password: str | None = None, timeout: float = 2.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,150}", index):
            raise ValueError("history index must be one concrete configured index")
        self.url, self.index_name = url.rstrip("/"), index
        self.timeout = max(.1, min(timeout, 10))
        self.auth = httpx.BasicAuth(username, password or "") if username else None
        self.transport = transport

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(timeout=self.timeout, auth=self.auth, transport=self.transport) as client:
                async with client.stream(method, f"{self.url}/{path}", json=body) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1_000_000:
                            raise ValueError("history response too large")
        document = json.loads(data)
        if response.is_error:
            error = document.get("error", {})
            raise HistoryIndexError(response.status_code, error.get("type", "unknown") if isinstance(error, dict) else "unknown")
        return document

    async def ensure_index(self) -> None:
        mapping = json.loads((Path(__file__).parent / "mapping.json").read_text(encoding="utf8"))
        try:
            await self.request("PUT", self.index_name, mapping)
        except HistoryIndexError as exc:
            # A concurrent worker may have created it; do not swallow mapping/auth errors.
            if exc.status != 400 or exc.kind != "resource_already_exists_exception":
                raise

    async def index(self, item: HistoryItem) -> None:
        await self.ensure_index()
        from app.history.sanitize import sanitize
        await self.request("PUT", f"{self.index_name}/_doc/{quote(item.history_id, safe='')}",
                           sanitize(item.model_dump(mode="json")))

    async def search(self, user_id: str, project_id: str, query: HistoryQuery) -> list[str]:
        filters: list[dict] = [{"term": {"user_id": user_id}}, {"term": {"project_id": project_id}},
                              {"term": {"service": query.service}}]
        if query.category:
            filters.append({"term": {"category": query.category}})
        if query.tags:
            filters.append({"terms": {"tags": query.tags}})
        period = {}
        if query.start_time:
            period["gte"] = query.start_time.isoformat()
        if query.end_time:
            period["lte"] = query.end_time.isoformat()
        if period:
            filters.append({"range": {"timestamp": period}})
        boolean: dict = {"filter": filters}
        if query.text.strip():
            boolean["must"] = [{"multi_match": {"query": query.text, "fields": ["symptom^3", "root_cause^2", "summary"],
                                                 "type": "best_fields", "operator": "or"}}]
        if query.exclude_task_id:
            boolean["must_not"] = [{"term": {"task_id": query.exclude_task_id}}]
        result = await self.request("POST", f"{self.index_name}/_search", {
            "query": {"bool": boolean}, "size": query.limit, "_source": False,
            "track_total_hits": False, "timeout": f"{int(self.timeout * 1000)}ms",
            "sort": [{"_score": "desc"}, {"timestamp": "desc"}, {"history_id": "asc"}],
        })
        if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
            raise ValueError("partial history search")
        return [str(hit["_id"]) for hit in result["hits"]["hits"][:query.limit]]
