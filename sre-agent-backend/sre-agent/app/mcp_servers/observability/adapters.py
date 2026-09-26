"""Bounded read-only Elasticsearch and SkyWalking 10.2 adapters.

Only fixed search/GraphQL documents are sent upstream. Credentials, indexes and
endpoints are configuration, never model-controlled tool arguments.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

TRACE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,256}$")


class QueryFailure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def window(arguments: dict[str, Any]) -> tuple[datetime, datetime]:
    minutes = int(arguments.get("time_range_minutes", 30))
    if not 1 <= minutes <= 1440:
        raise ValueError("time_range_minutes must be between 1 and 1440")
    def parse(value: Any) -> datetime:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timestamps require a timezone")
        return result.astimezone(timezone.utc)
    end = parse(arguments["end_time"]) if arguments.get("end_time") else datetime.now(timezone.utc)
    start = parse(arguments["start_time"]) if arguments.get("start_time") else end - timedelta(minutes=minutes)
    if not timedelta(0) < end - start <= timedelta(hours=24):
        raise ValueError("time range must be positive and at most 24 hours")
    return start, end


def text(value: Any, maximum: int = 256) -> str:
    value = str(value or "")
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError(f"filter must contain at most {maximum} printable characters")
    return value


def trace_id(value: Any) -> str:
    value = str(value or "")
    if not TRACE_ID.fullmatch(value):
        raise ValueError("invalid trace_id")
    return value


def row_limit(arguments: dict[str, Any], default: int) -> int:
    value = int(arguments.get("limit", default))
    if not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


class ReadAdapter:
    source: str

    def __init__(self, url: str, timeout: float, output_limit: int, *, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.output_limit = max(512, output_limit)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.auth: httpx.BasicAuth | None = None

    async def request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
        # Streaming bounds decoded bytes even if Content-Length is absent/incorrect.
        async with client.stream(method, url, headers=self.headers, auth=self.auth, **kwargs) as response:
            if response.status_code in {401, 403}:
                raise QueryFailure("AUTHENTICATION", "data source rejected authentication or read permission")
            response.raise_for_status()
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > 2_000_000:
                    raise QueryFailure("RESPONSE_TOO_LARGE", "upstream response exceeded 2 MB; narrow the query")
            return json.loads(body)

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    result = await self.query(client, arguments)
            result = {"success": True, "source": self.source, **result}
            # Remove whole records rather than serializing a partial JSON string.
            truncated = False
            for key in ("logs", "spans", "traces", "metrics"):
                rows = result.get(key)
                while isinstance(rows, list) and rows and len(json.dumps(result, ensure_ascii=False)) > self.output_limit:
                    rows.pop()
                    truncated = True
            result["truncated"] = truncated
            collections = [result[key] for key in ("logs", "spans", "traces", "metrics") if key in result]
            if collections:
                result["empty"] = not any(collections)
            return {"data": result, "truncated": truncated}
        except (TimeoutError, httpx.TimeoutException):
            return self.error("TIMEOUT", f"query timed out after {self.timeout:g} seconds")
        except QueryFailure as exc:
            return self.error(exc.kind, str(exc))
        except httpx.HTTPStatusError as exc:
            return self.error("UNAVAILABLE", f"data source returned HTTP {exc.response.status_code}")
        except httpx.HTTPError:
            return self.error("CONNECTION", "unable to connect to data source")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # No response bodies, credentials or stack traces in error messages.
            return self.error("INVALID_QUERY_OR_RESPONSE", "invalid parameters or unexpected data source response")

    def error(self, kind: str, message: str) -> dict[str, Any]:
        return {"success": False, "source": self.source, "error_type": kind, "message": message}

    async def query(self, client: httpx.AsyncClient, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class ElasticsearchTool(ReadAdapter):
    source = "elasticsearch"

    def __init__(self, url: str, timeout: float, output_limit: int, index: str,
                 username: str | None = None, password: str | None = None) -> None:
        super().__init__(url, timeout, output_limit)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_*.-]{0,150}", index) or index == "*":
            raise ValueError("configure a restricted Elasticsearch index pattern")
        self.index = index
        if username:
            self.auth = httpx.BasicAuth(username, password or "")

    async def query(self, client: httpx.AsyncClient, arguments: dict[str, Any]) -> dict[str, Any]:
        start, end = window(arguments)
        limit = row_limit(arguments, 100)
        filters: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}}]
        fields = {"service_name": arguments.get("service_name") or arguments.get("service"),
                  "level": arguments.get("level"), "trace_id": arguments.get("trace_id"),
                  "exception_type": arguments.get("exception_type")}
        for key, value in fields.items():
            if value:
                value = text(value)
                filters.append({"term": {key: value.upper() if key == "level" else value}})
        keyword = text(arguments.get("keyword"), 200)
        query: dict[str, Any] = {"bool": {"filter": filters}}
        if keyword:
            query["bool"]["must"] = [{"match": {"message": {"query": keyword, "operator": "and"}}}]
        allowed = ["@timestamp", "service", "service_name", "environment", "level", "message", "trace_id",
                   "span_id", "request_id", "host", "pod_name", "namespace", "exception_type", "stack_trace",
                   "fault_mode", "sql", "sql_text", "version"]
        body = await self.request(client, "POST", f"{self.url}/{quote(self.index, safe='*')}/_search", json={
            "query": query, "size": limit, "sort": [{"@timestamp": "desc"}], "track_total_hits": False,
            "timeout": f"{max(1, int(self.timeout * 1000))}ms", "_source": allowed,
        })
        if body.get("timed_out"):
            raise QueryFailure("TIMEOUT", "Elasticsearch timed out; partial hits are not conclusive evidence")
        if body.get("_shards", {}).get("failed", 0):
            raise QueryFailure("PARTIAL_RESULT", "Elasticsearch reported failed shards")
        rows = []
        for hit in body["hits"]["hits"][:limit]:
            raw = hit.get("_source", {})
            row = {key: str(raw[key])[:(1500 if key == "stack_trace" else 800)]
                   for key in allowed if raw.get(key) is not None}
            row["timestamp"] = row.pop("@timestamp", None)
            row["raw_reference"] = f"elasticsearch://{quote(str(hit.get('_index', '')))}/{quote(str(hit.get('_id', '')))}"
            rows.append(row)
        return {"logs": rows, "empty": not rows, "start_time": start.isoformat(), "end_time": end.isoformat()}


SERVICE_QUERY = "query Service($name:String!){searchService(serviceCode:$name){id name}}"
SEARCH_QUERY = """query Traces($condition:TraceQueryCondition!){queryBasicTraces(condition:$condition){
traces{segmentId endpointNames duration start isError traceIds}}}"""
TRACE_QUERY = """query Trace($id:ID!){queryTrace(traceId:$id){spans{
traceId segmentId spanId parentSpanId serviceCode endpointName startTime endTime type peer component layer isError
tags{key value} refs{traceId parentSegmentId parentSpanId}}}}"""
METRICS_QUERY = """query Metrics($entity:Entity!,$duration:Duration!){
latency:execExpression(expression:"service_resp_time",entity:$entity,duration:$duration){error results{values{id value}}}
throughput:execExpression(expression:"service_cpm",entity:$entity,duration:$duration){error results{values{id value}}}
success_rate:execExpression(expression:"service_sla",entity:$entity,duration:$duration){error results{values{id value}}}}"""


class SkyWalkingTool(ReadAdapter):
    source = "skywalking"

    def __init__(self, operation: str, url: str, timeout: float, output_limit: int,
                 zipkin_url: str = "", token: str | None = None) -> None:
        super().__init__(url, timeout, output_limit, token=token)
        self.operation = operation
        self.zipkin_url = zipkin_url.rstrip("/")

    async def graphql(self, client: httpx.AsyncClient, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = await self.request(client, "POST", f"{self.url}/graphql", json={"query": query, "variables": variables})
        if body.get("errors"):
            raise QueryFailure("QUERY_ERROR", "SkyWalking GraphQL query failed; check server version and permissions")
        return body["data"]

    async def query(self, client: httpx.AsyncClient, arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = arguments.get("trace_id")
        if self.operation == "get_trace" or identifier:
            identifier = trace_id(identifier)
            raw = (await self.graphql(client, TRACE_QUERY, {"id": identifier})).get("queryTrace") or {}
            spans = raw.get("spans", [])
            if spans:
                return self.normalize_native(identifier, spans)
            if self.zipkin_url and re.fullmatch(r"[0-9a-fA-F]{16,32}", identifier):
                try:
                    spans = await self.request(client, "GET", f"{self.zipkin_url}/api/v2/trace/{identifier}")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                else:
                    return self.normalize_zipkin(identifier, spans)
            return {"trace_id": identifier, "spans": [], "empty": True}
        start, end = window(arguments)
        service = text(arguments.get("service_name") or arguments.get("service"), 160)
        if not service:
            raise ValueError("service_name is required")
        duration = {"start": start.strftime("%Y-%m-%d %H%M"), "end": end.strftime("%Y-%m-%d %H%M"), "step": "MINUTE"}
        if self.operation == "get_service_metrics":
            raw = await self.graphql(client, METRICS_QUERY, {"entity": {"serviceName": service, "normal": True}, "duration": duration})
            rows = []
            for metric, result in raw.items():
                if result.get("error"):
                    raise QueryFailure("QUERY_ERROR", "SkyWalking metric expression failed")
                rows.extend({"name": metric, "unit": {"latency": "ms", "throughput": "calls/min", "success_rate": "basis_points"}[metric],
                             "timestamp": point.get("id"), "value": point.get("value")}
                            for series in result.get("results", []) for point in series.get("values", [])[:100]
                            if point.get("value") is not None)
            return {"service_name": service, "metrics": rows[:100], "empty": not rows,
                    "notice": "Native SkyWalking APM only; OTLP traces do not imply native service metrics"}
        limit = row_limit(arguments, 20)
        minimum = int(arguments.get("min_duration_ms") or 0)
        if minimum < 0:
            raise ValueError("min_duration_ms must be nonnegative")
        error_only = arguments.get("error_only", False)
        if not isinstance(error_only, bool):
            raise ValueError("error_only must be boolean")
        found = (await self.graphql(client, SERVICE_QUERY, {"name": service})).get("searchService")
        rows = []
        if found:
            condition = {"serviceId": found["id"], "queryDuration": duration, "minTraceDuration": minimum,
                         "traceState": "ERROR" if error_only else "ALL", "queryOrder": "BY_DURATION",
                         "paging": {"pageNum": 1, "pageSize": limit}}
            data = (await self.graphql(client, SEARCH_QUERY, {"condition": condition})).get("queryBasicTraces") or {}
            for item in data.get("traces", []):
                for identifier in item.get("traceIds", [])[:1]:
                    rows.append({"trace_id": identifier, "service": service, "name": ", ".join(item.get("endpointNames", []))[:300],
                                 "duration_ms": item["duration"], "status": "ERROR" if item.get("isError") else "OK", "timestamp": item["start"]})
        if self.zipkin_url:
            params = {"serviceName": service, "endTs": int(end.timestamp() * 1000),
                      "lookback": int((end-start).total_seconds() * 1000), "limit": limit}
            if minimum:
                params["minDuration"] = minimum * 1000
            if error_only:
                params["annotationQuery"] = "error"
            raw_traces = await self.request(client, "GET", f"{self.zipkin_url}/api/v2/traces", params=params)
            for spans in raw_traces[:limit]:
                if spans:
                    normalized = self.normalize_zipkin(spans[0]["traceId"], spans)
                    rows.append({key: value for key, value in normalized.items() if key != "spans"})
        rows.sort(key=lambda item: item.get("duration_ms", 0), reverse=True)
        return {"traces": rows[:limit], "empty": not rows}

    @staticmethod
    def normalize_native(identifier: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for span in spans:
            tags = {item["key"]: str(item.get("value") or "")[:800] for item in span.get("tags", [])[:30]}
            rows.append({"span_id": f"{span['segmentId']}:{span['spanId']}", "parent_span_id": span.get("parentSpanId"),
                         "service": span["serviceCode"], "operation": str(span.get("endpointName") or "")[:300],
                         "duration_ms": max(0, int(span["endTime"]) - int(span["startTime"])),
                         "start_ms": int(span["startTime"]), "end_ms": int(span["endTime"]),
                         "status": "ERROR" if span.get("isError") else "OK", "kind": span.get("type"),
                         "peer": str(span.get("peer") or "")[:256], "layer": span.get("layer"), "tags": tags})
        return SkyWalkingTool.trace_summary(identifier, rows)

    @staticmethod
    def normalize_zipkin(identifier: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for span in spans:
            tags = {key: str(value)[:800] for key, value in list(span.get("tags", {}).items())[:30]}
            start = int(span.get("timestamp", 0)) / 1000
            duration = int(span.get("duration", 0)) / 1000
            rows.append({"span_id": span.get("id"), "parent_span_id": span.get("parentId"),
                         "service": span.get("localEndpoint", {}).get("serviceName", "unknown"),
                         "operation": str(span.get("name", ""))[:300], "duration_ms": duration,
                         "start_ms": start, "end_ms": start + duration,
                         "status": "ERROR" if "error" in tags or tags.get("otel.status_code") == "ERROR" else "OK",
                         "kind": span.get("kind"), "peer": span.get("remoteEndpoint", {}).get("serviceName", ""),
                         "layer": "Database" if any(key in tags for key in ("db.system", "db.system.name")) else "Http", "tags": tags})
        return SkyWalkingTool.trace_summary(identifier, rows)

    @staticmethod
    def trace_summary(identifier: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"trace_id": identifier, "spans": [], "empty": True}
        root = min(rows, key=lambda row: row["start_ms"])
        duration = max(row["end_ms"] for row in rows) - root["start_ms"]
        rows.sort(key=lambda row: row["duration_ms"], reverse=True)
        return {"trace_id": identifier, "service": root["service"], "name": root["operation"],
                "duration_ms": duration, "status": "ERROR" if any(row["status"] == "ERROR" for row in rows) else "OK",
                "span_count": len(rows), "spans": rows[:100], "empty": False, "spans_truncated": len(rows) > 100}
