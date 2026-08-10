"""用户附件预签名、上传完成校验与会话绑定服务。"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from app.conversation.service import ConversationService
from app.core.database import ApplicationDatabase
from app.storage import MinioObjectStore


class UploadValidationError(ValueError):
    """上传参数、大小或对象所有权不符合约束。"""


class UploadNotFoundError(LookupError):
    """目标会话或已上传对象不存在。"""


class UploadService:
    """让浏览器直传 MinIO，同时保证 Key 只能落在当前用户会话前缀。"""

    def __init__(
        self,
        database: ApplicationDatabase,
        conversations: ConversationService,
        object_store: MinioObjectStore,
        *,
        max_bytes: int,
        presign_expire_minutes: int,
    ) -> None:
        self.database = database
        self.conversations = conversations
        self.object_store = object_store
        self.max_bytes = max_bytes
        self.presign_expire_minutes = max(1, min(presign_expire_minutes, 60))

    def create_presigned_upload(
        self,
        *,
        user_id: str,
        conversation_id: str,
        filename: str,
        content_type: str,
        size: int,
        kind: str,
    ) -> dict[str, str]:
        """验证会话和预计大小后，为单一不可猜测 Key 生成 PUT 签名。"""
        if not self.conversations.owns(user_id, conversation_id):
            raise UploadNotFoundError("conversation not found")
        if size < 1 or size > self.max_bytes:
            raise UploadValidationError(f"object size must be between 1 and {self.max_bytes} bytes")
        if kind not in {"file", "pasted_log"}:
            raise UploadValidationError("unsupported upload kind")
        safe_name = self._safe_filename(filename)
        oss_key = f"uploads/{user_id}/{conversation_id}/{kind}/{uuid4().hex}-{safe_name}"
        upload_url = self.object_store.presigned_put(oss_key, self.presign_expire_minutes)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.presign_expire_minutes)
        return {
            "oss_key": oss_key,
            "upload_url": upload_url,
            "expires_at": expires_at.isoformat(),
        }

    def complete(
        self, *, user_id: str, conversation_id: str, oss_key: str, expected_size: int
    ) -> dict[str, Any]:
        """确认 MinIO 对象真实存在且大小一致后，持久化唯一 ``oss_key`` 关系。"""
        if not self.conversations.owns(user_id, conversation_id):
            raise UploadNotFoundError("conversation not found")
        expected_prefix = f"uploads/{user_id}/{conversation_id}/"
        if not oss_key.startswith(expected_prefix):
            raise UploadValidationError("object key does not belong to current conversation")
        metadata = self.object_store.stat(oss_key)
        if metadata.size != expected_size:
            raise UploadValidationError("uploaded object size does not match presign request")
        if metadata.size < 1 or metadata.size > self.max_bytes:
            raise UploadValidationError("uploaded object exceeds size limit")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_attachments(
                    id, conversation_id, user_id, oss_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (uuid4().hex, conversation_id, user_id, oss_key, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
            connection.commit()
        return {
            "oss_key": metadata.oss_key,
            "size": metadata.size,
            "content_type": metadata.content_type,
        }

    def create_download_url(self, *, user_id: str, oss_key: str) -> dict[str, str]:
        """只有附件所有者可换取新下载签名，旧签名到期后无法继续使用。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_attachments WHERE user_id = ? AND oss_key = ?",
                (user_id, oss_key),
            ).fetchone()
        if row is None:
            raise UploadNotFoundError("attachment not found")
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.presign_expire_minutes)
        return {
            "oss_key": oss_key,
            "download_url": self.object_store.presigned_get(oss_key, self.presign_expire_minutes),
            "expires_at": expires_at.isoformat(),
        }

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """去除浏览器可能提交的本地路径，并把不安全字符替换为下划线。"""
        leaf = PurePath(filename.replace("\\", "/")).name.strip()
        normalized = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", leaf)[:160]
        if normalized in {"", ".", ".."}:
            return "upload.bin"
        return normalized
