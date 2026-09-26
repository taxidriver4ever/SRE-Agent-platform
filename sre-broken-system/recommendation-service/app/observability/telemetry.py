"""Prometheus instruments ranking latency, cache size and request outcomes."""
import json
import logging
import time
import os
import socket
from app.observability.skywalking import correlation
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
    native_trace, span = correlation()
    payload = {"@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "service": LABELS[0], "service_name": LABELS[0], "version": LABELS[1],
               "pod": LABELS[2], "pod_name": LABELS[2], "level": "INFO", "message": message,
               "environment": os.getenv("ENVIRONMENT", "development"),
               "namespace": os.getenv("KUBERNETES_NAMESPACE", "sre-lab"), "host": socket.gethostname(),
               "request_id": None, "exception_type": None, "stack_trace": None, **fields,
               "trace_id": native_trace or trace_id or None, "span_id": span}
    logger.info(json.dumps(payload))
