"""Prometheus instruments ranking latency, cache size and request outcomes."""
import json
import logging
import time
from prometheus_client import Counter, Gauge, Histogram
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("recommendation-service")
LABELS = ("recommendation-service", settings.version, settings.pod_name)
REQUESTS = Counter("sre_http_requests_total", "HTTP requests", ["service", "version", "pod", "path", "status"])
LATENCY = Histogram("sre_http_request_duration_seconds", "HTTP latency", ["service", "version", "pod", "path"], buckets=(.01,.05,.1,.5,1,5))
CACHE_SIZE = Gauge("sre_recommendation_cache_entries", "Bounded recommendation cache size", ["service", "version", "pod"])


def log_event(message: str, trace_id: str, **fields: object) -> None:
    """Emit common version- and trace-aware JSON logs."""
    logger.info(json.dumps({"timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"service":LABELS[0],"version":LABELS[1],"pod":LABELS[2],"level":"INFO","trace_id":trace_id,"message":message,**fields}))
