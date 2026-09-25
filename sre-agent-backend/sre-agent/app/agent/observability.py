"""Local framework events correlated with the existing business Timeline/Audit."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.callbacks import AsyncCallbackHandler

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


class LocalCallTrace(AsyncCallbackHandler):
    """Record public lifecycle metadata only; raw evidence stays in existing stores."""

    def __init__(self, metadata):
        self.metadata = metadata
        self.started = {}

    async def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._start("tool", run_id, (serialized or {}).get("name"))

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._start("llm", run_id, "sre-gateway")

    def _start(self, kind, run_id, name):
        self.started[run_id] = time.perf_counter()
        logger.info("agent.call.started %s", {
            **self.metadata, "kind": kind, "name": name, "framework_run_id": str(run_id),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

    async def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id, "success")

    async def on_llm_end(self, response, *, run_id, **kwargs):
        self._end(run_id, "success")

    async def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "failed", type(error).__name__)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "failed", type(error).__name__)

    def _end(self, run_id, status, error_type=None):
        started = self.started.pop(run_id, time.perf_counter())
        logger.info("agent.call.finished %s", {
            **self.metadata, "framework_run_id": str(run_id), "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.perf_counter() - started) * 1000), "error_type": error_type,
        })


def call_config():
    scope = current_task_scope()
    metadata = {**({"task_id": scope.task_id, "project_id": scope.project_id} if scope else {}),
                **(_context.get() or {})}
    return {"metadata": metadata, "callbacks": [LocalCallTrace(metadata)]}
