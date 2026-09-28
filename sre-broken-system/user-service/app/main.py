"""FastAPI application factory with health, metrics and request instrumentation."""

from app.observability.http import install

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


install(app, "user-service", REQUESTS, LATENCY, settings)

@app.get("/health")
async def health() -> dict[str, str]:
    """Kubernetes probe includes version and fault state for Pod-level diagnosis."""
    return {"status": "ok", "service": "user-service", "version": settings.version,
            "pod": settings.pod_name, "fault_mode": faults.get()}

@app.get("/ready")
async def ready():
    from app.repository.user_repository import engine
    from sqlalchemy import text
    from starlette.concurrency import run_in_threadpool
    def query():
        with engine.connect() as connection: connection.execute(text("SELECT 1"))
    await run_in_threadpool(query)
    return {"status":"ok"}
