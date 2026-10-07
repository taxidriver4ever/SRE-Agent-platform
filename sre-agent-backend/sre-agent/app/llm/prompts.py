"""Shared prompt composition. Values are data, never nested format templates."""

from app.llm.base import LLMMessage


def prompt_messages(system: str, user: str) -> list[LLMMessage]:
    return [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)]
