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

    def __init__(self, repository: TokenRepository) -> None:
        """注入 Repository，使业务层与具体数据库实现解耦。"""
        self.repository = repository

    def generate(self) -> GeneratedToken:
        """生成随机 Token，并且只把不可逆 Hash 交给 Repository 保存。"""
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        created_at = _now()
        self.repository.create(_hash_token(token), created_at)
        return GeneratedToken(token=token, created_at=created_at)

    def validate(self, token: str) -> TokenPrincipal | None:
        """校验 Token 格式、是否存在以及是否仍处于启用状态。"""
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            return None

        record = self.repository.find_enabled_by_hash(_hash_token(token))
        if record is None:
            return None
        return TokenPrincipal(token_id=record.id, created_at=record.created_at)

    def disable(self, token: str) -> bool:
        """禁用一个有效 Token；未知、错误或已禁用时返回 ``False``。"""
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            return False
        return self.repository.disable_by_hash(_hash_token(token), _now())


def hash_token(token: str) -> str:
    """公开 Hash 辅助函数，仅供测试或数据迁移验证使用。"""
    return _hash_token(token)


def _hash_token(token: str) -> str:
    """返回 Token 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    """返回包含 UTC 时区的 ISO 8601 时间字符串。"""
    return datetime.now(UTC).isoformat()

