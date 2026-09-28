"""W3C server/client spans, bounded OTLP export and uniform HTTP diagnostics."""
import json, logging, os, re, time, uuid
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_client import Gauge
from app.observability.telemetry import request_id_context

active = Gauge("sre_http_active_requests", "In-flight requests")
def install(app, service, requests, latency, settings):
    provider = TracerProvider(resource=Resource.create({"service.name": service,"service.version":settings.version}))
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() != "true":
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint+"/v1/traces",timeout=1),max_queue_size=512))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    tracer = trace.get_tracer(service)
    @app.middleware("http")
    async def observe(request: Request, call_next):
        started = time.perf_counter()
        rid = request.headers.get("x-request-id", "")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}",rid): rid = str(uuid.uuid4())
        request.state.request_id = rid
        request_token = request_id_context.set(rid)
        active.inc()
        with tracer.start_as_current_span(request.method,context=propagate.extract(request.headers),kind=trace.SpanKind.SERVER) as span:
            sc = span.get_span_context(); request.state.trace_id = format(sc.trace_id,"032x")
            try:
                response = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                logging.error(json.dumps({"service":service,"event":"request_failed","error_type":type(exc).__name__,"trace_id":request.state.trace_id,"request_id":rid}))
                response = JSONResponse({"code":"SERVICE_UNAVAILABLE","message":"service temporarily unavailable","request_id":rid},status_code=503)
            finally:
                active.dec()
                request_id_context.reset(request_token)
            route = getattr(request.scope.get("route"),"path","unmatched")
            elapsed = time.perf_counter()-started
            requests.labels(service,settings.version,settings.pod_name,route,str(response.status_code)).inc()
            latency.labels(service,settings.version,settings.pod_name,route).observe(elapsed)
            span.update_name(request.method+" "+route)
            span.set_attributes({"http.method":request.method,"http.route":route,"http.status_code":response.status_code,"request.id":rid})
            if response.status_code>=500: span.set_status(trace.StatusCode.ERROR)
            logging.info(json.dumps({"timestamp":time.time(),"level":"INFO","environment":"lab","service":service,"trace_id":request.state.trace_id,"span_id":format(sc.span_id,"016x"),"request_id":rid,"endpoint":route,"latency_ms":elapsed*1000,"status":response.status_code,"event":"http_request"}))
            response.headers.update({"X-Request-ID":rid,"X-Service-Version":settings.version,"X-Pod-Name":settings.pod_name})
            return response
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = "DEPENDENCY_TIMEOUT" if exc.status_code == 504 else "BUSINESS_ERROR"
        return JSONResponse({"code":code,"message":str(exc.detail),"request_id":request.state.request_id},status_code=exc.status_code)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code":"INVALID_REQUEST","message":"invalid request parameters","request_id":request.state.request_id},status_code=422)
