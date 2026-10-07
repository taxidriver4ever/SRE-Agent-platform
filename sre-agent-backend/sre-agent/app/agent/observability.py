"""Local call metadata correlated with business Timeline/Audit; no payload logging."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from uuid import uuid4
import asyncio

from app.security import current_task_scope

logger = logging.getLogger(__name__)
_context = ContextVar("sre_agent_call_context", default=None)


@contextmanager
def agent_call_context(**values):
    token = _context.set({**(_context.get() or {}), **values})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def trace_call(kind: str, name: str):
    """Log one call locally and propagate every exception, including cancellation."""
    scope = current_task_scope()
    metadata = {**({"task_id": scope.task_id, "project_id": scope.project_id} if scope else {}),
                **(_context.get() or {}), "kind": kind, "name": name, "call_id": uuid4().hex}
    started = time.perf_counter()
    logger.info("agent.call.started %s", {
        **metadata, "started_at": datetime.now(timezone.utc).isoformat(),
    })
    status, error_type = "success", None
    try:
        yield
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        error_type = type(exc).__name__
        raise
    finally:
        logger.info("agent.call.finished %s", {
            **metadata, "status": status, "error_type": error_type,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        })
