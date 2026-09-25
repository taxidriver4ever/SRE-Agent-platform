"""Shared prompt composition. Values are data, never nested format templates."""

from langchain_core.prompts import ChatPromptTemplate

from app.llm.base import LLMMessage
from app.llm.langchain import from_messages


_PAIR = ChatPromptTemplate.from_messages([
    ("system", "{system}"), ("user", "{user}"),
])


def prompt_messages(system: str, user: str) -> list[LLMMessage]:
    return from_messages(_PAIR.format_messages(system=system, user=user))
