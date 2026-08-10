"""基于 SQLite 的密码认证和可撤销不透明 Token 实现。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase


PBKDF2_ITERATIONS = 600_000


class AuthService:
    """负责创建本地用户、校验密码、签发/验证/撤销数据库 Token。"""

    def __init__(self, database: ApplicationDatabase, token_ttl_hours: int = 24) -> None:
        self.database = database
        self.token_ttl_hours = max(1, token_ttl_hours)

    def ensure_user(self, username: str, password: str) -> None:
        """首次启动时创建本地管理员；已有用户名绝不被环境变量静默改密。"""
        normalized = username.strip()
        if not normalized or len(password) < 6:
            raise ValueError("初始用户名不能为空，密码至少 6 个字符")
        with closing(self.database.connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
            if exists:
                return
            connection.execute(
                "INSERT INTO users(id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (uuid4().hex, normalized, self._hash_password(password), self._now()),
            )
            connection.commit()

    def login(self, username: str, password: str) -> dict[str, Any] | None:
        """统一返回 None 表示用户名或密码错误，避免泄露账号是否存在。"""
        with closing(self.database.connect()) as connection:
            user = connection.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
            if user is None or not self._verify_password(password, str(user["password_hash"])):
                return None
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.token_ttl_hours)
            connection.execute(
                """
                INSERT INTO auth_tokens(id, user_id, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (uuid4().hex, user["id"], self._token_hash(token), self._now(), expires_at.isoformat()),
            )
            connection.commit()
            return {
                "access_token": token,
                "expires_at": expires_at.isoformat(),
                "user": {"id": str(user["id"]), "username": str(user["username"])},
            }

    def authenticate(self, token: str) -> dict[str, str] | None:
        """校验摘要、撤销状态和 UTC 到期时间，并返回最小用户身份。"""
        if not token or len(token) > 512:
            return None
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username, t.expires_at
                FROM auth_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = ? AND t.revoked_at IS NULL
                """,
                (self._token_hash(token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
                return None
            return {"id": str(row["id"]), "username": str(row["username"])}

    def logout(self, token: str) -> None:
        """只撤销当前 Token，用户在其他设备上的独立会话不受影响。"""
        with closing(self.database.connect()) as connection:
            connection.execute(
                "UPDATE auth_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (self._now(), self._token_hash(token)),
            )
            connection.commit()

    @staticmethod
    def _hash_password(password: str) -> str:
        """使用独立 128-bit 盐和 60 万轮 PBKDF2-HMAC-SHA256 保存密码。"""
        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return "pbkdf2_sha256${}${}${}".format(
            PBKDF2_ITERATIONS,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        """解析数据库编码并用 constant-time compare 防止时序侧信道。"""
        try:
            algorithm, iterations, salt_text, expected_text = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _token_hash(token: str) -> str:
        """数据库只保存 Token 摘要，数据库泄露时不能直接重放登录会话。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
