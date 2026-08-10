"""Conversation 与 Message 的用户隔离持久化服务。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase


class ConversationService:
    """所有查询都携带 user_id，防止通过猜测 conversation_id 越权读取。"""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def create(self, user_id: str, title: str) -> dict[str, Any]:
        """创建归属于当前用户的空会话，并返回列表所需摘要。"""
        conversation_id = uuid4().hex
        now = self._now()
        normalized_title = " ".join(title.split())[:120] or "新诊断"
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO conversations(id, user_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, user_id, normalized_title, now, now),
            )
            connection.commit()
        return {
            "id": conversation_id,
            "title": normalized_title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def ensure(self, user_id: str, conversation_id: str | None, first_message: str) -> str:
        """复用用户自己的会话；没有 ID 时以首条问题自动创建。"""
        if conversation_id:
            if self._owned_conversation(user_id, conversation_id) is None:
                raise KeyError("conversation not found")
            return conversation_id
        return str(self.create(user_id, first_message[:60])["id"])

    def list_for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """按最近更新时间加载轻量摘要，用于前端进入页面后的会话缓存。"""
        bounded_limit = max(1, min(limit, 100))
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN conversation_messages m ON m.conversation_id = c.id
                WHERE c.user_id = ?
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (user_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        """返回归属校验后的会话和消息；JSON 解码失败不会静默返回脏数据。"""
        conversation = self._owned_conversation(user_id, conversation_id)
        if conversation is None:
            return None
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, role, content_json, created_at
                FROM conversation_messages
                WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
            attachment_rows = connection.execute(
                """
                SELECT oss_key, created_at
                FROM conversation_attachments
                WHERE conversation_id = ? AND user_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id, user_id),
            ).fetchall()
        messages = [{
            "id": str(row["id"]),
            "role": str(row["role"]),
            "content": json.loads(str(row["content_json"])),
            "created_at": str(row["created_at"]),
        } for row in rows]
        attachments = [dict(row) for row in attachment_rows]
        return {
            **conversation,
            "message_count": len(messages),
            "messages": messages,
            "attachments": attachments,
        }

    def owns(self, user_id: str, conversation_id: str) -> bool:
        """供 UploadService 复用同一用户隔离规则，不暴露会话内容。"""
        return self._owned_conversation(user_id, conversation_id) is not None

    def validate_attachment_keys(
        self, user_id: str, conversation_id: str, oss_keys: list[str]
    ) -> list[str]:
        """确认请求中的每个 Key 都已上传完成且属于当前会话。"""
        unique_keys = list(dict.fromkeys(oss_keys))
        if not unique_keys:
            return []
        placeholders = ",".join("?" for _ in unique_keys)
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT oss_key FROM conversation_attachments
                WHERE user_id = ? AND conversation_id = ? AND oss_key IN ({placeholders})
                """,
                (user_id, conversation_id, *unique_keys),
            ).fetchall()
        found = {str(row["oss_key"]) for row in rows}
        if found != set(unique_keys):
            raise KeyError("attachment not found in current conversation")
        return unique_keys

    def append(self, user_id: str, conversation_id: str, role: str, content: Any) -> str:
        """在同一事务中验证所有权、写入消息并推进会话更新时间。"""
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        message_id = uuid4().hex
        now = self._now()
        serialized = json.dumps(content, ensure_ascii=False, default=str)
        with closing(self.database.connect()) as connection:
            owned = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if owned is None:
                raise KeyError("conversation not found")
            connection.execute(
                """
                INSERT INTO conversation_messages(id, conversation_id, role, content_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, conversation_id, role, serialized, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
            connection.commit()
        return message_id

    def _owned_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        """只按 user_id + id 查询，不提供跨用户的管理员旁路。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "message_count": 0}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
