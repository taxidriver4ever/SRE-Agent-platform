"""FastAPI composition root and request metrics middleware."""
import secrets
import time
from fastapi import FastAPI,Request
from prometheus_client import make_asgi_app
from app.api.routes import router,service
from app.core.config import settings
from app.observability.telemetry import LATENCY,LABELS,REQUESTS

app=FastAPI(title="SRE Lab Recommendation Service",version=settings.version);app.mount("/metrics",make_asgi_app());app.include_router(router)

@app.middleware("http")
async def observe(request:Request,call_next):
    """Attach trace identity and collect a version/pod HTTP histogram."""
    started=time.perf_counter();parts=request.headers.get("traceparent","").split("-");request.state.trace_id=parts[1]if len(parts)==4 else secrets.token_hex(16);response=await call_next(request);route=request.scope.get("route");path=getattr(route,"path",request.url.path);REQUESTS.labels(*LABELS,path,str(response.status_code)).inc();LATENCY.labels(*LABELS,path).observe(time.perf_counter()-started);response.headers["X-Service-Version"]=settings.version;return response

@app.get("/health")
def health()->dict[str,str]:
    """Probe exposes version and per-Pod mode for Agent comparison."""
    return{"status":"ok","service":"recommendation-service","version":settings.version,"pod":settings.pod_name,"fault_mode":service.mode()}
