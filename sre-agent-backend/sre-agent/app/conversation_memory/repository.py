"""只访问 Conversation 压缩表和专用记忆表的 MySQL Repository。"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.conversation_memory.models import CompactionOutput, MemoryItemDraft, ShortContextState
from app.core.database import ApplicationDatabase


@dataclass(slots=True)
class ActiveContextSnapshot:
    summary: str
    state: ShortContextState
    pending_messages: list[dict[str, Any]]


class ConversationMemoryRepository:
    """所有读写同时限定 user_id 与 conversation_id。"""

    ALLOWED_ITEM_TYPES = {
        "goal", "user_intent", "constraint", "confirmed_finding", "hypothesis", "decision",
        "open_question", "next_action", "evidence", "reference",
    }

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def active_snapshot(self, user_id: str, conversation_id: str) -> ActiveContextSnapshot:
        with closing(self.database.connect()) as connection:
            self._require_owned(connection, user_id, conversation_id)
            latest = connection.execute(
                """
                SELECT conversation_summary, context_state_json, compacted_through_message_id
                FROM conversation_compactions
                WHERE user_id = ? AND conversation_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (user_id, conversation_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT id, role, message_type, content_json, estimated_tokens,
                       run_id, tool_name, created_at
                FROM conversation_messages
                WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        messages = [
            {
                "id": str(row["id"]),
                "role": str(row["role"]),
                "message_type": str(row["message_type"]),
                "content": json.loads(str(row["content_json"])),
                "estimated_tokens": int(row["estimated_tokens"]),
                "run_id": str(row["run_id"]) if row["run_id"] else None,
                "tool_name": str(row["tool_name"]) if row["tool_name"] else None,
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
        cutoff = str(latest["compacted_through_message_id"]) if latest else None
        start = 0
        if cutoff:
            for index, message in enumerate(messages):
                if message["id"] == cutoff:
                    start = index + 1
                    break
        return ActiveContextSnapshot(
            summary=str(latest["conversation_summary"]) if latest else "",
            state=(
                ShortContextState.model_validate_json(str(latest["context_state_json"]))
                if latest else ShortContextState()
            ),
            pending_messages=messages[start:],
        )

    def commit(
        self,
        user_id: str,
        conversation_id: str,
        output: CompactionOutput,
        through_message_id: str,
        input_token_count: int,
    ) -> str:
        """一次事务提交 Summary、State、Memory Item 和压缩边界。"""
        compaction_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.database.connect()) as connection:
            self._require_owned(connection, user_id, conversation_id)
            owned_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM conversation_messages WHERE conversation_id = ?",
                    (conversation_id,),
                )
            }
            if through_message_id not in owned_ids:
                raise ValueError("compaction boundary is outside the current conversation")
            invalid_refs = {
                item.source_message_id
                for item in output.memory_items
                if item.source_message_id and item.source_message_id not in owned_ids
            }
            if invalid_refs:
                raise ValueError("memory item references a message outside the current conversation")
            connection.execute(
                """
                INSERT INTO conversation_compactions(
                    id, user_id, conversation_id, conversation_summary, context_state_json,
                    compacted_through_message_id, input_token_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compaction_id, user_id, conversation_id, output.conversation_summary,
                    output.context_state.model_dump_json(), through_message_id,
                    input_token_count, now,
                ),
            )
            # 新 State 是当前状态的完整快照；上一版同类条目退出 active，但历史行保留。
            state_types = (
                "goal", "user_intent", "constraint", "confirmed_finding", "hypothesis", "decision",
                "open_question", "next_action",
            )
            placeholders = ",".join("?" for _ in state_types)
            connection.execute(
                f"""
                UPDATE conversation_memory_items
                SET status = 'superseded', updated_at = ?
                WHERE user_id = ? AND conversation_id = ? AND status = 'active'
                  AND item_type IN ({placeholders})
                """,
                [now, user_id, conversation_id, *state_types],
            )
            for item in [*self._state_items(output.context_state), *output.memory_items]:
                fingerprint = hashlib.sha256(
                    f"{item.item_type}:{item.content}:{item.source_message_id or ''}".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO conversation_memory_items(
                        id, user_id, conversation_id, compaction_id, item_type, title,
                        content, status, importance, source_message_id, source_tool_name,
                        fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        compaction_id = VALUES(compaction_id),
                        title = VALUES(title),
                        status = VALUES(status),
                        importance = VALUES(importance),
                        source_tool_name = VALUES(source_tool_name),
                        updated_at = VALUES(updated_at)
                    """,
                    (
                        uuid4().hex, user_id, conversation_id, compaction_id,
                        item.item_type, item.title, item.content, item.status,
                        item.importance, item.source_message_id, item.source_tool_name,
                        fingerprint, now, now,
                    ),
                )
            connection.commit()
        return compaction_id

    @staticmethod
    def _state_items(state: ShortContextState) -> list[MemoryItemDraft]:
        """确定性地把短 State 展开到唯一允许模型查询的 Memory 表。"""
        items: list[MemoryItemDraft] = []
        if state.goal:
            items.append(MemoryItemDraft(item_type="goal", title="当前目标", content=state.goal, importance=1.0))
        if state.user_intent:
            items.append(MemoryItemDraft(
                item_type="user_intent", title="用户意图", content=state.user_intent, importance=0.95
            ))
        field_map = {
            "constraints": ("constraint", "约束", 0.95),
            "confirmed_findings": ("confirmed_finding", "已确认事实", 0.95),
            "hypotheses": ("hypothesis", "待验证假设", 0.7),
            "decisions": ("decision", "已作决定", 0.85),
            "open_questions": ("open_question", "未决问题", 0.8),
            "next_actions": ("next_action", "下一步", 0.8),
        }
        for field, (item_type, title, importance) in field_map.items():
            for value in getattr(state, field):
                items.append(MemoryItemDraft(
                    item_type=item_type,
                    title=title,
                    content=value,
                    importance=importance,
                ))
        return items

    def search(
        self,
        user_id: str,
        conversation_id: str,
        query: str,
        item_types: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """固定查询 conversation_memory_items；调用方不能提供 SQL 或表名。"""
        selected_types = [item for item in (item_types or []) if item in self.ALLOWED_ITEM_TYPES]
        bounded_limit = max(1, min(limit, 20))
        pattern = f"%{query[:200]}%"
        type_clause = ""
        parameters: list[Any] = [user_id, conversation_id, pattern, pattern]
        if selected_types:
            placeholders = ",".join("?" for _ in selected_types)
            type_clause = f" AND item_type IN ({placeholders})"
            parameters.extend(selected_types)
        parameters.append(bounded_limit)
        sql = f"""
            SELECT id, item_type, title, content, status, importance,
                   source_message_id, source_tool_name, updated_at
            FROM conversation_memory_items
            WHERE user_id = ? AND conversation_id = ? AND status = 'active'
              AND (title LIKE ? OR content LIKE ?)
              {type_clause}
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
        """
        with closing(self.database.connect()) as connection:
            self._require_owned(connection, user_id, conversation_id)
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def compaction_count(self, user_id: str, conversation_id: str) -> int:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM conversation_compactions
                WHERE user_id = ? AND conversation_id = ?
                """,
                (user_id, conversation_id),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _require_owned(connection: Any, user_id: str, conversation_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError("conversation not found")
