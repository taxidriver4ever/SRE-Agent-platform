"""Common Prometheus, JSON logging and lightweight OTLP tracing integration."""

import json
import logging
import time
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import settings


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("user-service")
REQUESTS = Counter("sre_http_requests_total", "HTTP requests", ["service", "version", "pod", "path", "status"])
LATENCY = Histogram("sre_http_request_duration_seconds", "HTTP latency", ["service", "version", "pod", "path"])
BLOCKING = Gauge("sre_python_blocking_operation", "Whether a blocking experiment is active", ["service", "version", "pod"])


def log_event(level: str, message: str, trace_id: str = "", **fields: object) -> None:
    """Emit the same JSON envelope used by Java, Go and Node services."""
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service": "user-service", "version": settings.version, "pod": settings.pod_name,
        "level": level, "trace_id": trace_id, "message": message, **fields,
    }
    logger.log(getattr(logging, level, logging.INFO), json.dumps(payload, ensure_ascii=False))
