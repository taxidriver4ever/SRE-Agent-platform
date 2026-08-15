"""API Token 的核心业务规则。

Service 负责生成、Hash、格式校验和状态判断，但不直接依赖 SQLAlchemy；所有
持久化操作统一委托给 ``TokenRepository``。
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from app.auth.repository import TokenRepository
from app.operation_log import OperationLogEntry, OperationLogRepository

TOKEN_PREFIX = "gw_sk_"
TOKEN_PATTERN = re.compile(r"^gw_sk_[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class GeneratedToken:
    """新生成 Token 的一次性返回值，明文不会交给 Repository。"""

    token: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    """Token 校验成功后向上层提供的最小身份信息。"""

    token_id: int
    created_at: str


class TokenService:
    """提供 Token 生成、校验和禁用业务操作。"""

    def __init__(
        self,
        repository: TokenRepository,
        operation_repository: OperationLogRepository | None = None,
    ) -> None:
        """注入 Repository，使业务层与具体数据库实现解耦。"""
        self.repository = repository
        self.operation_repository = operation_repository

    def generate(self) -> GeneratedToken:
        """生成随机 Token，并且只把不可逆 Hash 交给 Repository 保存。"""
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        created_at = _now()
        record = self.repository.create(_hash_token(token), created_at)
        self._record_operation("api_key.create", record.id, True, 201, "Gateway API Key 已创建")
        return GeneratedToken(token=token, created_at=created_at)

    def validate(self, token: str) -> TokenPrincipal | None:
        """校验 Token 格式、是否存在以及是否仍处于启用状态。"""
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            self._record_operation("api_key.authenticate", None, False, 401, "API Key 格式无效")
            return None

        record = self.repository.find_enabled_by_hash(_hash_token(token))
        if record is None:
            self._record_operation("api_key.authenticate", None, False, 401, "API Key 不存在或已禁用")
            return None
        self._record_operation("api_key.authenticate", record.id, True, 200, "API Key 鉴权成功")
        return TokenPrincipal(token_id=record.id, created_at=record.created_at)

    def disable(self, token: str) -> bool:
        """禁用一个有效 Token；未知、错误或已禁用时返回 ``False``。"""
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            self._record_operation("api_key.disable", None, False, 400, "API Key 格式无效")
            return False
        token_hash = _hash_token(token)
        record = self.repository.find_enabled_by_hash(token_hash)
        disabled = self.repository.disable_by_hash(token_hash, _now())
        self._record_operation(
            "api_key.disable",
            record.id if record is not None else None,
            disabled,
            200 if disabled else 404,
            "Gateway API Key 已禁用" if disabled else "API Key 不存在或已禁用",
        )
        return disabled

    def _record_operation(
        self,
        operation: str,
        token_id: int | None,
        success: bool,
        status_code: int,
        detail: str,
    ) -> None:
        """记录不包含 API Key 明文的安全操作事件。"""
        if self.operation_repository is None:
            return
        self.operation_repository.create(
            OperationLogEntry(
                operation=operation,
                token_id=token_id,
                request_id=None,
                success=success,
                status_code=status_code,
                detail=detail,
                created_at=_now(),
            )
        )


def hash_token(token: str) -> str:
    """公开 Hash 辅助函数，仅供测试或数据迁移验证使用。"""
    return _hash_token(token)


def _hash_token(token: str) -> str:
    """返回 Token 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    """返回包含 UTC 时区的 ISO 8601 时间字符串。"""
    return datetime.now(UTC).isoformat()
