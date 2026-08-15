"""Gateway 操作审计日志 Repository。"""

from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.database import Database
from app.operation_log.model import GatewayOperationLog


@dataclass(frozen=True, slots=True)
class OperationLogEntry:
    """不包含 API Key、Prompt 或模型回复的审计事件。"""

    operation: str
    token_id: int | None
    request_id: str | None
    success: bool
    status_code: int
    detail: str | None
    created_at: str


def initialize_operation_log_tables(database: Database) -> None:
    """从 Operation Log 模块 SQL 文件幂等创建审计日志表。"""
    database.execute_schema_file(Path(__file__).resolve().parent / "sql" / "schema.sql")


class OperationLogRepository:
    """持久化 Gateway 安全操作审计事件。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, entry: OperationLogEntry) -> None:
        with self.database.session() as session:
            session.add(GatewayOperationLog(**asdict(entry)))
            session.commit()
