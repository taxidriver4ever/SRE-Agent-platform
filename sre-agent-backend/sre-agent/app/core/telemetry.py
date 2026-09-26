"""JSON logging with real SkyWalking context; no fabricated trace identities."""

import json
import logging
import os
import socket
from datetime import datetime, timezone


def current_trace() -> tuple[str | None, str | None]:
    try:
        from skywalking.trace.context import get_context
        context = get_context()
        span = context.active_span
        if span is not None:
            return str(context.segment.related_traces[0]), str(span.sid)
    except ImportError:
        pass
    return None, None


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        trace, span = current_trace()
        service = os.getenv("SKYWALKING_SERVICE_NAME", "sre-agent")
        payload = {
            "@timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "service": service, "service_name": service,
            "environment": os.getenv("ENVIRONMENT", "development"),
            "level": record.levelname, "message": record.getMessage(),
            "trace_id": trace, "span_id": span, "request_id": getattr(record, "request_id", None),
            "host": socket.gethostname(), "pod_name": os.getenv("POD_NAME"),
            "namespace": os.getenv("KUBERNETES_NAMESPACE"),
            "exception_type": record.exc_info[0].__name__ if record.exc_info else None,
            "stack_trace": self.formatException(record.exc_info) if record.exc_info else None,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
