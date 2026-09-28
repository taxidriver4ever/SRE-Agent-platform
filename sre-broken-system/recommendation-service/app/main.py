"""FastAPI composition root and request metrics middleware."""
from app.observability.http import install

import secrets
import time
from fastapi import FastAPI,Request
from prometheus_client import make_asgi_app
from app.api.routes import router,service
from app.core.config import settings
from app.observability.telemetry import LATENCY,LABELS,REQUESTS

app=FastAPI(title="SRE Lab Recommendation Service",version=settings.version);app.mount("/metrics",make_asgi_app());app.include_router(router)

install(app, "recommendation-service", REQUESTS, LATENCY, settings)

@app.get("/health")
def health()->dict[str,str]:
    """Probe exposes version and per-Pod mode for Agent comparison."""
    return{"status":"ok","service":"recommendation-service","version":settings.version,"pod":settings.pod_name,"fault_mode":service.mode()}

@app.get("/ready")
async def ready():
    return {"status":"ok"}
