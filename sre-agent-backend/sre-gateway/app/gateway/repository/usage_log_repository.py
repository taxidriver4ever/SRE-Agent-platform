"""Gateway Usage/Logs 的 SQLAlchemy Repository。"""

from dataclasses import dataclass
from pathlib import Path

from app.core.database import Database
from app.gateway.model import GatewayUsageLog


@dataclass(frozen=True, slots=True)
class UsageLogEntry:
    """Service 传给 Repository 的无敏感内容日志对象。"""

    request_id: str
    client_api_key_id: int
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    success: bool
    status_code: int
    error_message: str | None
    created_at: str


def initialize_gateway_tables(database: Database) -> None:
    """幂等创建 Gateway 模块负责的 Usage/Logs 表。"""
    database.execute_schema_file(Path(__file__).resolve().parents[1] / "sql" / "schema.sql")


class UsageLogRepository:
    """负责持久化每次 Provider 调用的结果指标。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, entry: UsageLogEntry) -> None:
        """把一次调用指标写入 SQLite，不保存 Prompt 或模型输出。"""
        total_tokens = entry.prompt_tokens + entry.completion_tokens
        with self.database.session() as session:
            session.add(
                GatewayUsageLog(
                    request_id=entry.request_id,
                    # 这里只记录用户 Gateway Key 的内部 ID，不记录 Key 明文，
                    # 更不会记录任何 Provider API Key。
                    token_id=entry.client_api_key_id,
                    provider=entry.provider,
                    model=entry.model,
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=entry.completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=entry.latency_ms,
                    success=entry.success,
                    status_code=entry.status_code,
                    error_message=entry.error_message,
                    created_at=entry.created_at,
                )
            )
            session.commit()
