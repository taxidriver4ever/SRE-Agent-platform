"""Gateway 操作审计日志 ORM 模型。"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GatewayOperationLog(Base):
    """记录 API Key 与 Gateway 调用的安全审计事件。"""

    __tablename__ = "gateway_operation_logs"
    __table_args__ = {
        "comment": "Gateway API Key 和模型调用操作审计日志，不保存 Key、Prompt 或回复明文"
    }

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="操作日志自增主键")
    operation: Mapped[str] = mapped_column(String(64), nullable=False, comment="操作类型")
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="关联的 Gateway API Key 内部 ID")
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="关联的 Gateway 请求 ID")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="操作是否成功")
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, comment="操作结果状态码")
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="不含敏感信息的操作结果摘要")
    created_at: Mapped[str] = mapped_column(String, nullable=False, comment="操作时间，带 UTC 时区的 ISO 8601 字符串")
