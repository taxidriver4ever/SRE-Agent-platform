"""Real HTTP documents, normalized evidence and causal workflow regression."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.mcp_servers.observability.adapters import ElasticsearchTool, SkyWalkingTool
from app.evidence import normalize_tool_result
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.models import DiagnosisState
from app.workflow.planning.decision_rules import evidence_driven_decision
from app.workflow.planning.synthesis_rules import deterministic_synthesis
from app.workflow.evidence_gate import build_report


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return install


def es(limit=12000):
    return ElasticsearchTool("http://es", 3, limit, "sre-logs-*", "reader", "secret")


def sw(operation="query_trace", zipkin=""):
    return SkyWalkingTool(operation, "http://oap:12800", 3, 12000, zipkin)


def execute(adapter, arguments):
    return asyncio.run(adapter.execute(arguments))


def test_elasticsearch_filters_and_fixed_read_endpoint(transport):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={"hits":{"hits":[{"_index":"sre-logs-2026.09.25","_id":"1","_source":{
            "service_name":"order-service","@timestamp":"2026-09-25T02:00:00Z","message":"SQL timeout",
            "level":"ERROR","trace_id":"abc123","exception_type":"TimeoutException"}}]}})
    transport(handler)
    result=execute(es(),{"service_name":"order-service","start_time":"2026-09-25T01:00:00Z",
                         "end_time":"2026-09-25T02:00:00Z","level":"error","keyword":"SQL timeout",
                         "trace_id":"abc123","exception_type":"TimeoutException","limit":7})
    assert result["data"]["logs"][0]["trace_id"]=="abc123"
    request=calls[0];body=json.loads(request.content)
    assert request.method=="POST" and request.url.path=="/sre-logs-*/_search"
    assert body["size"]==7 and body["timeout"]=="3000ms"
    assert {"term":{"service_name":"order-service"}} in body["query"]["bool"]["filter"]
    assert {"term":{"level":"ERROR"}} in body["query"]["bool"]["filter"]
    assert {"term":{"trace_id":"abc123"}} in body["query"]["bool"]["filter"]
    assert body["query"]["bool"]["must"][0]["match"]["message"]["query"]=="SQL timeout"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("arguments",[{"limit":0},{"limit":101},{"time_range_minutes":1441},
    {"start_time":"2026-01-01T00:00:00Z","end_time":"2026-01-03T00:00:00Z"},
    {"start_time":"2026-01-03T00:00:00Z","end_time":"2026-01-01T00:00:00Z"},
    {"start_time":"2026-01-01"},{"keyword":"x"*201}])
def test_invalid_queries_do_not_reach_upstream(transport,arguments):
    def unexpected(request):raise AssertionError("must validate before IO")
    transport(unexpected)
    assert execute(es(),arguments)["success"] is False


@pytest.mark.parametrize("factory",[es,lambda:sw("search_traces")])
@pytest.mark.parametrize("status,kind",[(401,"AUTHENTICATION"),(403,"AUTHENTICATION"),(503,"UNAVAILABLE")])
def test_http_errors_are_compact_and_structured(transport,factory,status,kind):
    transport(lambda request:httpx.Response(status,text="secret upstream stack trace"))
    result=execute(factory(),{"service_name":"order-service"})
    assert result["error_type"]==kind
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("factory",[es,lambda:sw("get_trace")])
def test_timeout_is_structured(transport,factory):
    def handler(request):raise httpx.ReadTimeout("sensitive endpoint")
    transport(handler)
    result=execute(factory(),{"trace_id":"abc123"})
    assert result["error_type"]=="TIMEOUT"


def test_empty_and_partial_logs(transport):
    transport(lambda request:httpx.Response(200,json={"hits":{"hits":[]}}))
    assert execute(es(),{})["data"]["empty"] is True
    transport(lambda request:httpx.Response(200,json={"timed_out":True,"hits":{"hits":[]}}))
    assert execute(es(),{})["error_type"]=="TIMEOUT"


def test_log_truncation_keeps_records_structured(transport):
    transport(lambda request:httpx.Response(200,json={"hits":{"hits":[{"_source":{"message":"x"*3000,"trace_id":"abc123"}}]*100}}))
    result=execute(es(1500),{})
    assert result["truncated"] and isinstance(result["data"]["logs"],list)
    assert len(json.dumps(result["data"],ensure_ascii=False))<1600


def native_spans():
    return [
        {"segmentId":"s1","spanId":0,"parentSpanId":-1,"serviceCode":"order-service","endpointName":"POST /order",
         "startTime":1000,"endTime":4100,"type":"Entry","isError":True,"tags":[]},
        {"segmentId":"s1","spanId":1,"parentSpanId":0,"serviceCode":"order-service","endpointName":"SELECT orders",
         "startTime":1100,"endTime":3900,"type":"Exit","layer":"Database","peer":"mysql:3306","isError":True,
         "tags":[{"key":"db.statement","value":"SELECT id FROM orders"}]},
    ]


def test_native_trace_duration_and_slow_span(transport):
    def handler(request):
        body=json.loads(request.content)
        assert body["variables"]=={"id":"abc123"}
        assert "queryTrace" in body["query"] and "mutation" not in body["query"]
        return httpx.Response(200,json={"data":{"queryTrace":{"spans":native_spans()}}})
    transport(handler)
    result=execute(sw("get_trace"),{"trace_id":"abc123"})["data"]
    assert result["duration_ms"]==3100 and result["status"]=="ERROR"
    assert result["spans"][1]["duration_ms"]==2800
    normalized=normalize_tool_result("query_trace",{"trace_id":"abc123"},{"data":result},[])
    assert normalized.structured_data["dependency_candidates"][0]["service"]=="mysql"
    assert normalized.structured_data["sql_statements"]==["SELECT id FROM orders"]


def test_search_resolves_real_service_id_and_filters(transport):
    def handler(request):
        body=json.loads(request.content)
        if "searchService" in body["query"]:
            return httpx.Response(200,json={"data":{"searchService":{"id":"opaque-id","name":"order-service"}}})
        condition=body["variables"]["condition"]
        assert condition["serviceId"]=="opaque-id" and condition["traceState"]=="ERROR"
        assert condition["minTraceDuration"]==1000 and condition["paging"]["pageSize"]==5
        return httpx.Response(200,json={"data":{"queryBasicTraces":{"traces":[{"traceIds":["abc123"],"endpointNames":["POST /order"],"duration":3100,"start":"1000","isError":True}]}}})
    transport(handler)
    data=execute(sw("search_traces"),{"service_name":"order-service","error_only":True,"min_duration_ms":1000,"limit":5})["data"]
    assert data["traces"][0]["trace_id"]=="abc123"


def test_zipkin_compatibility_retains_w3c_id_and_microsecond_units(transport):
    identifier="a"*32
    def handler(request):
        if request.url.path=="/graphql":return httpx.Response(200,json={"data":{"queryTrace":{"spans":[]}}})
        assert request.url.path==f"/zipkin/api/v2/trace/{identifier}"
        return httpx.Response(200,json=[{"traceId":identifier,"id":"b"*16,"timestamp":1000000,"duration":2800000,
            "name":"SELECT orders","kind":"CLIENT","localEndpoint":{"serviceName":"order-service"},"tags":{"db.system":"mysql","error":"timeout"}}])
    transport(handler)
    result=execute(sw(zipkin="http://oap:9412/zipkin"),{"trace_id":identifier})["data"]
    assert result["trace_id"]==identifier and result["duration_ms"]==2800


def test_graphql_error_and_malformed_response(transport):
    transport(lambda request:httpx.Response(200,json={"errors":[{"message":"private detail"}]}))
    assert execute(sw("get_trace"),{"trace_id":"abc123"})["error_type"]=="QUERY_ERROR"
    transport(lambda request:httpx.Response(200,text="not json"))
    assert execute(sw("get_trace"),{"trace_id":"abc123"})["success"] is False


def test_empty_native_trace(transport):
    transport(lambda request:httpx.Response(200,json={"data":{"queryTrace":None}}))
    assert execute(sw("get_trace"),{"trace_id":"abc123"})["data"]["empty"] is True


def test_oversized_response_rejected(transport):
    transport(lambda request:httpx.Response(200,content=b"x"*2_000_001))
    assert execute(es(),{})["error_type"]=="RESPONSE_TOO_LARGE"


def test_metrics_document_is_fixed_and_bounded(transport):
    def handler(request):
        body=json.loads(request.content)
        assert body["variables"]["entity"]=={"serviceName":"order-service","normal":True}
        assert 'expression:"service_resp_time"' in body["query"]
        return httpx.Response(200,json={"data":{"latency":{"error":None,"results":[{"values":[{"id":"2026-09-25 1200","value":"3100"}]}]}}})
    transport(handler)
    assert execute(sw("get_service_metrics"),{"service_name":"order-service"})["data"]["metrics"][0]["unit"]=="ms"


def test_metrics_logs_trace_workflow_builds_database_latency_evidence_chain(transport):
    def handler(request):
        if request.url.host=="es":
            return httpx.Response(200,json={"hits":{"hits":[{"_source":{"service_name":"order-service","level":"ERROR","message":"SQL timeout","trace_id":"abc123"}}]}})
        return httpx.Response(200,json={"data":{"queryTrace":{"spans":native_spans()}}})
    transport(handler)
    class Tools:
        async def execute(self,name,arguments):
            if name=="query_metrics":
                return {"data":{"result":{"result":[{"metric":{"service":"order-service"},"value":[1,"3"]}]}}}
            return await (es() if name=="query_logs" else sw()).execute(arguments)
    async def run():
        workflow=object.__new__(DiagnosisWorkflow)
        workflow.tools=Tools();workflow.max_steps=12;workflow.conversation_service=None;workflow.kubernetes_namespace="sre-lab"
        state=DiagnosisState(query="order-service P99 latency",service="order-service",symptom="latency")
        await workflow._call(state,"query_metrics",{"query":"p99"},"P99",None)
        await workflow._call(state,"query_logs",{"service":"order-service"},"Errors",None)
        decision=evidence_driven_decision(state)
        assert decision.tool_name=="query_trace" and decision.arguments["trace_id"]=="abc123"
        await workflow._call(state,decision.tool_name,decision.arguments,"Trace",None,parent_evidence_ids=decision.parent_evidence_ids)
        state.synthesis=deterministic_synthesis(state)
        return build_report(state)
    report=asyncio.run(run())
    assert report.status=="confirmed"
    assert "数据库调用" in report.root_cause and "2800" in report.root_cause
    assert {item.source for item in report.evidence}=={"Prometheus","Elasticsearch","SkyWalking"}
    assert report.evidence[-1].parent_evidence_ids


def test_source_failure_cannot_pass_evidence_gate(transport):
    transport(lambda request:httpx.Response(503))
    normalized=normalize_tool_result("query_logs",{},execute(es(),{}),[])
    assert normalized.status=="error"


def test_zipkin_search_omits_zero_minimum(transport):
    def handler(request):
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"searchService": None}})
        assert "minDuration" not in request.url.params
        return httpx.Response(200, json=[])
    transport(handler)
    result = execute(sw("search_traces", zipkin="http://oap:9412/zipkin"), {"service_name": "demo"})
    assert result["data"]["empty"] is True


def test_null_metrics_are_not_evidence(transport):
    transport(lambda request: httpx.Response(200, json={"data": {
        "latency": {"results": [{"values": [{"id": "1", "value": None}]}]}}}))
    result = execute(sw("get_service_metrics"), {"service_name": "missing"})
    assert result["data"]["empty"] is True
    assert result["data"]["metrics"] == []


def test_trimming_all_records_marks_result_empty(transport):
    transport(lambda request: httpx.Response(200, json={"hits": {"hits": [
        {"_source": {"message": "x" * 800, "service_name": "demo"}}]}}))
    result = execute(es(limit=512), {})
    assert result["data"]["truncated"] is True
    assert result["data"]["empty"] is True


def test_observability_metadata_survives_durable_evidence_roundtrip():
    from app.diagnosis.execution import DurableWorkflowRuntime
    from app.workflow.models import Evidence
    runtime = object.__new__(DurableWorkflowRuntime)
    runtime.diagnosis_id = "migration-test"
    original = Evidence(source="SkyWalking", tool_name="get_trace", title="Trace", detail="db latency",
        timestamp=datetime.now(timezone.utc), evidence_id="trace-1",
        evidence_type="trace", service_name="order-service", trace_id="abc123", severity="ERROR",
        raw_reference="skywalking://trace/abc123", parent_evidence_ids=["log-1"],
        source_references=[{"kind": "trace", "uri": "skywalking://trace/abc123", "label": "Trace abc123"}],
        structured_data={"trace_ids": ["abc123"]}, supports_conclusion=True)
    persisted = runtime._diagnosis_evidence(original, {}, "trace-step")
    restored = runtime._workflow_evidence(persisted)
    for field in ("source", "evidence_type", "service_name", "trace_id", "severity", "raw_reference",
                  "parent_evidence_ids", "source_references", "structured_data", "supports_conclusion"):
        assert getattr(restored, field) == getattr(original, field)
