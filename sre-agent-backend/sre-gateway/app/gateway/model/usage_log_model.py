"""Gateway 调用 Usage 与结果日志 ORM 表。"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GatewayUsageLog(Base):
    """记录一次模型调用的用量、耗时与成功状态。

    出于隐私和安全考虑，本表不保存用户 messages、模型回复或 API Key。
    """

    __tablename__ = "gateway_usage_logs"
    __table_args__ = {
        "comment": "LLM Gateway 模型调用用量与结果日志表，不保存 Prompt、回复或 API Key"
    }

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
        comment="调用日志的数据库自增主键",
    )
    request_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="Gateway 为本次模型调用生成的唯一请求 ID，用于排查和链路追踪",
    )
    token_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="发起调用的用户 Gateway API Key 内部 ID；不是模型厂商 API Key，且不包含 Key 明文",
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="实际接收请求的模型厂商，例如 openai、claude、deepseek、vllm 或 ollama",
    )
    model: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="路由后实际调用的厂商模型名称",
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="厂商统计的输入 Token 数量",
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="厂商统计的模型输出 Token 数量",
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="本次调用总 Token 数，等于输入 Token 数加输出 Token 数",
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="从调用 Provider 到收到结果或错误的总耗时，单位为毫秒",
    )
    success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="本次 Provider 调用是否成功；true 为成功，false 为失败",
    )
    status_code: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="调用结果状态码；成功通常为 200，失败时记录 Provider 或配置错误状态码",
    )
    error_message: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="调用失败时的简化错误信息；成功时为空，不包含 Prompt 或 API Key",
    )
    created_at: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="调用日志创建时间，保存为带 UTC 时区的 ISO 8601 字符串",
    )
