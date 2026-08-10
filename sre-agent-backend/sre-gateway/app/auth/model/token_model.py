"""Gateway Token 的 SQLAlchemy ORM 表定义。"""

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GatewayToken(Base):
    """持久化 Gateway API Token 的 Hash 与启用状态。

    数据库绝不保存 Token 明文。``disabled_at`` 为空表示 Token 有效，写入时间
    后表示 Token 已禁用。
    """

    __tablename__ = "gateway_tokens"
    __table_args__ = (
        # SHA-256 的十六进制摘要固定为 64 个字符。
        CheckConstraint("length(token_hash) = 64", name="ck_token_hash_length"),
        {"comment": "LLM Gateway API Token 鉴权凭证表，仅保存 Token Hash 和启停状态"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
        comment="Token 记录的数据库自增主键，也是鉴权成功后的内部 Token ID",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="API Token 明文经过 SHA-256 计算后的 64 位十六进制 Hash，数据库不保存明文",
    )
    created_at: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Token 创建时间，保存为带 UTC 时区的 ISO 8601 字符串",
    )
    disabled_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Token 禁用时间；为空表示 Token 有效，非空表示已禁用",
    )
