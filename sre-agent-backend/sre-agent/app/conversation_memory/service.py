"""80% 上下文预算触发、结构化重试与 MySQL 原子压缩。"""

from __future__ import annotations

import json
from typing import Any

from app.conversation_memory.models import CompactionOutput, ShortContextState
from app.conversation_memory.repository import ActiveContextSnapshot, ConversationMemoryRepository
from app.llm.base import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError,
    schema_retry_message,
    template_refill_message,
    validate_structured_output,
)


class ConversationCompactionService:
    """以 MySQL 为单一事实源；失败时绝不推进压缩边界。"""

    def __init__(
        self,
        repository: ConversationMemoryRepository,
        llm: LLM,
        *,
        model_context_window: int = 32768,
        compaction_ratio: float = 0.80,
        reserved_output_tokens: int = 4096,
        structured_output_retries: int = 3,
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.model_context_window = max(4096, model_context_window)
        self.compaction_ratio = min(0.95, max(0.50, compaction_ratio))
        self.reserved_output_tokens = max(512, reserved_output_tokens)
        self.structured_output_retries = max(2, min(3, structured_output_retries))
        self.last_failed_raw_output: dict[str, str] = {}

    @staticmethod
    def estimate_tokens(value: str) -> int:
        return max(1, (len(value) + 2) // 3)

    def active_context(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        snapshot = self.repository.active_snapshot(user_id, conversation_id)
        return self._document(snapshot)

    def build_active_context(self, user_id: str, conversation_id: str) -> str:
        return json.dumps(
            self.active_context(user_id, conversation_id), ensure_ascii=False, default=str
        )

    def active_token_count(self, user_id: str, conversation_id: str) -> int:
        return self.estimate_tokens(self.build_active_context(user_id, conversation_id))

    def usage_ratio(self, user_id: str, conversation_id: str) -> float:
        occupied = self.active_token_count(user_id, conversation_id) + self.reserved_output_tokens
        return occupied / self.model_context_window

    def should_compact(self, user_id: str, conversation_id: str) -> bool:
        snapshot = self.repository.active_snapshot(user_id, conversation_id)
        return bool(snapshot.pending_messages) and self.usage_ratio(user_id, conversation_id) >= self.compaction_ratio

    async def maybe_compact(
        self,
        user_id: str,
        conversation_id: str,
        *,
        force: bool = False,
    ) -> CompactionOutput | None:
        snapshot = self.repository.active_snapshot(user_id, conversation_id)
        if not snapshot.pending_messages or (not force and not self.should_compact(user_id, conversation_id)):
            return None
        input_document = self._document(snapshot)
        input_tokens = self.estimate_tokens(json.dumps(input_document, ensure_ascii=False, default=str))
        messages = [
            LLMMessage(
                "system",
                "你是会话压缩器。把旧对话合并为短 Summary、短 Context State 和最多20条可检索记忆。"
                "State 只保留当前目标、约束、确认事实、有效假设、决定、未决问题和下一步；"
                "完整日志不得复制到 State 或 memory_items。Evidence 必须短且用 source_message_id 引用原始消息。"
                "只输出符合 Schema 的 JSON，不输出思维链。/no_think",
            ),
            LLMMessage("user", json.dumps(input_document, ensure_ascii=False, default=str)),
        ]
        original_output = ""
        output: CompactionOutput | None = None
        for attempt in range(self.structured_output_retries + 1):
            response = await self.llm.complete(messages)
            if attempt == 0:
                original_output = response.content
            messages.append(LLMMessage("assistant", response.content))
            try:
                output = validate_structured_output(response.content, CompactionOutput)
                break
            except StructuredOutputError as exc:
                if attempt < self.structured_output_retries:
                    messages.append(LLMMessage("user", schema_retry_message(exc)))
        if output is None:
            template = CompactionOutput(
                conversation_summary="",
                context_state=ShortContextState(),
                memory_items=[],
            ).model_dump(mode="json")
            messages.append(LLMMessage("user", template_refill_message(template, original_output)))
            refill = await self.llm.complete(messages)
            try:
                output = validate_structured_output(refill.content, CompactionOutput)
            except StructuredOutputError:
                self.last_failed_raw_output[conversation_id] = original_output
                return None
        through_id = str(snapshot.pending_messages[-1]["id"])
        self.repository.commit(user_id, conversation_id, output, through_id, input_tokens)
        self.last_failed_raw_output.pop(conversation_id, None)
        return output

    @staticmethod
    def _document(snapshot: ActiveContextSnapshot) -> dict[str, Any]:
        return {
            "conversation_summary": snapshot.summary,
            "context_state": snapshot.state.model_dump(mode="json"),
            "recent_history": [
                {
                    "id": item["id"],
                    "role": item["role"],
                    "message_type": item["message_type"],
                    "content": item["content"],
                    "run_id": item["run_id"],
                    "tool_name": item["tool_name"],
                }
                for item in snapshot.pending_messages
            ],
        }
