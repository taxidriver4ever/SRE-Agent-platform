"""Common Prometheus, JSON logging and lightweight OTLP tracing integration."""

import json
import logging
import time
import os
import socket
from app.observability.skywalking import correlation
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import settings


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("user-service")
REQUESTS = Counter("sre_http_requests_total", "HTTP requests", ["service", "version", "pod", "path", "status"])
LATENCY = Histogram("sre_http_request_duration_seconds", "HTTP latency", ["service", "version", "pod", "path"])
BLOCKING = Gauge("sre_python_blocking_operation", "Whether a blocking experiment is active", ["service", "version", "pod"])


def log_event(level: str, message: str, trace_id: str = "", **fields: object) -> None:
    """Emit the same JSON envelope used by Java, Go and Node services."""
    native_trace, span = correlation()
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service": "user-service", "version": settings.version, "pod": settings.pod_name,
        "level": level, "message": message, **fields,
        "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service_name": "user-service", "environment": os.getenv("ENVIRONMENT", "development"),
        "trace_id": native_trace or trace_id or None, "span_id": span,
        "request_id": fields.get("request_id"), "host": socket.gethostname(),
        "pod_name": settings.pod_name, "namespace": os.getenv("KUBERNETES_NAMESPACE", "sre-lab"),
        "exception_type": fields.get("exception_type"), "stack_trace": fields.get("stack_trace"),
    }
    logger.log(getattr(logging, level, logging.INFO), json.dumps(payload, ensure_ascii=False))
