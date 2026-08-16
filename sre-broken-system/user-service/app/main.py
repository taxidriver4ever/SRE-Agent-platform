"""FastAPI application factory with health, metrics and request instrumentation."""

import secrets
import time
from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from app.api.routes import router
from app.core.config import settings
from app.core.faults import faults
from app.observability.telemetry import LATENCY, REQUESTS


app = FastAPI(title="SRE Lab User Service", version=settings.version)
app.state.version = settings.version
app.state.pod_name = settings.pod_name
app.mount("/metrics", make_asgi_app())
app.include_router(router)


@app.middleware("http")
async def observe_request(request: Request, call_next):
    """Measure all HTTP routes and reuse the incoming W3C trace ID in structured logs."""
    started = time.perf_counter()
    traceparent = request.headers.get("traceparent", "")
    parts = traceparent.split("-")
    request.state.trace_id = parts[1] if len(parts) == 4 else secrets.token_hex(16)
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    REQUESTS.labels("user-service", settings.version, settings.pod_name, path, str(response.status_code)).inc()
    LATENCY.labels("user-service", settings.version, settings.pod_name, path).observe(time.perf_counter() - started)
    response.headers["X-Service-Version"] = settings.version
    response.headers["X-Pod-Name"] = settings.pod_name
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    """Kubernetes probe includes version and fault state for Pod-level diagnosis."""
    return {"status": "ok", "service": "user-service", "version": settings.version,
            "pod": settings.pod_name, "fault_mode": faults.get()}
