"""Tool Audit Log 的只追加 Repository。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase
from app.security.models import TaskSecurityScope


_SENSITIVE_PARTS = ("password", "secret", "token", "authorization", "credential", "api_key")


class ToolAuditRepository:
    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def record(
        self,
        scope: TaskSecurityScope,
        tool_name: str,
        parameters: dict[str, Any],
        result_status: str,
        execution_time_ms: int,
        error_type: str | None = None,
    ) -> str:
        if result_status not in {"success", "failed", "denied"}:
            raise ValueError("unsupported audit result_status")
        audit_id = uuid4().hex
        sanitized = _sanitize(parameters)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_audit_logs(
                    id, user_id, project_id, task_id, tool_name, parameters_json,
                    result_status, execution_time_ms, error_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    scope.user_id,
                    scope.project_id,
                    scope.task_id,
                    tool_name,
                    json.dumps(sanitized, ensure_ascii=False, default=str),
                    result_status,
                    max(0, execution_time_ms),
                    error_type[:120] if error_type else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        return audit_id


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_PARTS)
            else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:4000]
    return value
