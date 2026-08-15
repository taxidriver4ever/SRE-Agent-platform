"""MySQL Conversation Compaction、短 State 与受限 Memory 检索回归测试。"""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.conversation import ConversationService
from app.conversation_memory import ConversationCompactionService, ConversationMemoryRepository
from app.conversation_memory.models import CompactionOutput, ShortContextState
from app.llm.base import LLMMessage, LLMResponse
from tests.mysql_support import mysql_test_database


class FakeCompactionLLM:
    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        self.calls.append(messages.copy())
        value = self.responses.pop(0)
        return LLMResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            "fake-model",
            "test",
        )


@pytest.fixture
def memory_stack():
    database = mysql_test_database()
    with closing(database.connect()) as connection:
        for user_id in ("user-a", "user-b"):
            connection.execute(
                "INSERT INTO users(id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, user_id, "hash", "2026-01-01T00:00:00+00:00"),
            )
        connection.commit()
    return database, ConversationService(database), ConversationMemoryRepository(database)


def valid_compaction(source_message_id: str) -> dict[str, object]:
    return {
        "conversation_summary": "用户正在调查订单接口延迟，已经完成数据库检查。",
        "context_state": {
            "goal": "定位订单延迟",
            "user_intent": "找到可验证根因",
            "constraints": ["只读调查"],
            "confirmed_findings": ["慢 SQL 已确认"],
            "hypotheses": [],
            "decisions": [],
            "open_questions": ["哪个提交引入查询"],
            "next_actions": ["检查运行版本源码"],
        },
        "memory_items": [{
            "item_type": "evidence",
            "title": "慢 SQL",
            "content": "订单查询未命中索引并发生全表扫描",
            "status": "active",
            "importance": 0.9,
            "source_message_id": source_message_id,
            "source_tool_name": "explain_sql",
        }],
    }


def test_normal_phase_keeps_messages_tool_calls_and_results_in_active_context(memory_stack):
    _, conversations, repository = memory_stack
    conversation_id = conversations.create("user-a", "订单延迟")["id"]
    conversations.append("user-a", conversation_id, "user", {"message": "为什么慢"})
    conversations.append(
        "user-a", conversation_id, "assistant", {"tool_name": "query_logs", "arguments": {}},
        message_type="tool_call", run_id="run-1", tool_name="query_logs",
    )
    conversations.append(
        "user-a", conversation_id, "assistant", {"result": {"logs": "full raw log"}},
        message_type="tool_result", run_id="run-1", tool_name="query_logs",
    )
    service = ConversationCompactionService(
        repository, FakeCompactionLLM([]), model_context_window=32768,
        compaction_ratio=0.8, reserved_output_tokens=512,
    )

    active = service.active_context("user-a", conversation_id)

    assert [item["message_type"] for item in active["recent_history"]] == [
        "user", "tool_call", "tool_result"
    ]
    assert active["recent_history"][2]["content"]["result"]["logs"] == "full raw log"


def test_compaction_triggers_when_active_context_reaches_configured_ratio(memory_stack):
    _, conversations, repository = memory_stack
    conversation_id = conversations.create("user-a", "预算")["id"]
    conversations.append("user-a", conversation_id, "user", {"message": "x" * 9000})
    service = ConversationCompactionService(
        repository, FakeCompactionLLM([]), model_context_window=4096,
        compaction_ratio=0.8, reserved_output_tokens=512,
    )

    assert service.usage_ratio("user-a", conversation_id) >= 0.8
    assert service.should_compact("user-a", conversation_id) is True


@pytest.mark.asyncio
async def test_compaction_persists_short_state_memory_and_keeps_original_messages(memory_stack):
    database, conversations, repository = memory_stack
    conversation_id = conversations.create("user-a", "订单延迟")["id"]
    conversations.append("user-a", conversation_id, "user", {"message": "为什么慢"})
    source_id = conversations.append(
        "user-a", conversation_id, "assistant", {"result": {"plan": "ALL"}},
        message_type="tool_result", run_id="run-1", tool_name="explain_sql",
    )
    service = ConversationCompactionService(
        repository, FakeCompactionLLM([valid_compaction(source_id)])
    )

    output = await service.maybe_compact("user-a", conversation_id, force=True)

    assert output is not None
    assert output.context_state.goal == "定位订单延迟"
    assert repository.compaction_count("user-a", conversation_id) == 1
    assert service.active_context("user-a", conversation_id)["recent_history"] == []
    assert repository.search("user-a", conversation_id, "全表扫描", ["evidence"], 10)[0]["source_message_id"] == source_id
    assert repository.search("user-a", conversation_id, "定位订单", ["goal"], 10)[0]["item_type"] == "goal"
    with closing(database.connect()) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_failed_template_refill_keeps_compaction_boundary_unchanged(memory_stack):
    _, conversations, repository = memory_stack
    conversation_id = conversations.create("user-a", "失败保护")["id"]
    conversations.append("user-a", conversation_id, "user", {"message": "保留原文"})
    invalid = '{"context_state":{"constraints":"wrong"}}'
    service = ConversationCompactionService(repository, FakeCompactionLLM([invalid] * 5))

    output = await service.maybe_compact("user-a", conversation_id, force=True)

    assert output is None
    assert repository.compaction_count("user-a", conversation_id) == 0
    assert len(service.active_context("user-a", conversation_id)["recent_history"]) == 1
    assert service.last_failed_raw_output[conversation_id] == invalid


def test_short_state_rejects_long_or_excessive_content():
    with pytest.raises(ValidationError):
        ShortContextState(goal="x" * 121)
    with pytest.raises(ValidationError):
        ShortContextState(constraints=["x"] * 6)
    with pytest.raises(ValidationError):
        ShortContextState(confirmed_findings=["x" * 121])


def test_memory_search_isolated_by_user_and_conversation(memory_stack):
    _, conversations, repository = memory_stack
    conversation_a = conversations.create("user-a", "A")["id"]
    conversation_b = conversations.create("user-b", "B")["id"]
    source_a = conversations.append("user-a", conversation_a, "user", {"message": "A"})
    source_b = conversations.append("user-b", conversation_b, "user", {"message": "B"})
    repository.commit(
        "user-a", conversation_a, CompactionOutput.model_validate(valid_compaction(source_a)),
        source_a, 10,
    )
    repository.commit(
        "user-b", conversation_b, CompactionOutput.model_validate(valid_compaction(source_b)),
        source_b, 10,
    )

    own = repository.search("user-a", conversation_a, "全表扫描", None, 20)

    assert len(own) == 1
    assert own[0]["source_message_id"] == source_a
    with pytest.raises(KeyError):
        repository.search("user-a", conversation_b, "全表扫描", None, 20)
