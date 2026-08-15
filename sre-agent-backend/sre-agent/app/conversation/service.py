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
                SELECT id, role, message_type, content_json, run_id, tool_name, created_at
                FROM conversation_messages
                WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        messages = [{
            "id": str(row["id"]),
            "role": str(row["role"]),
            "message_type": str(row["message_type"]),
            "content": json.loads(str(row["content_json"])),
            "run_id": str(row["run_id"]) if row["run_id"] else None,
            "tool_name": str(row["tool_name"]) if row["tool_name"] else None,
            "created_at": str(row["created_at"]),
        } for row in rows]
        return {
            **conversation,
            "message_count": len(messages),
            "messages": messages,
        }

    def append(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        content: Any,
        *,
        message_type: str | None = None,
        run_id: str | None = None,
        tool_name: str | None = None,
    ) -> str:
        """在同一事务中验证所有权、写入消息并推进会话更新时间。"""
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        normalized_type = message_type or role
        if normalized_type not in {"user", "assistant", "tool_call", "tool_result"}:
            raise ValueError("unsupported conversation message type")
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
                INSERT INTO conversation_messages(
                    id, conversation_id, role, message_type, content_json,
                    estimated_tokens, run_id, tool_name, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, conversation_id, role, normalized_type, serialized,
                    self.estimate_tokens(serialized), run_id, tool_name, now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
            connection.commit()
        return message_id

    def get_tool_result(
        self,
        user_id: str,
        run_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """只返回当前用户会话中的指定 Tool Result，禁止跨会话按 ID 回读。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT m.id, m.content_json, m.created_at, m.tool_name
                FROM conversation_messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.id = ? AND c.user_id = ?
                  AND m.run_id = ? AND m.message_type = 'tool_result'
                """,
                (message_id, user_id, run_id),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["content_json"]))
        return {
            "evidence_id": str(row["id"]),
            "tool_name": str(row["tool_name"] or payload.get("tool_name") or ""),
            "arguments": payload.get("arguments", {}),
            "result": payload.get("result"),
            "source_references": payload.get("source_references", []),
            "stored_at": str(row["created_at"]),
        }

    @staticmethod
    def estimate_tokens(value: str) -> int:
        """V1 使用稳定保守近似：约每三个字符一个 Token。"""
        return max(1, (len(value) + 2) // 3)

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
