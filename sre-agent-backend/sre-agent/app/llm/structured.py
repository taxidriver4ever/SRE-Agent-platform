"""Bounded structured generation shared by Intent, Planner and compaction.

Only common mechanics live here. Callers own their distinct fallback/commit
rules. This does not introduce model/tool retries beyond the existing budgets.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.output_parsers import BaseOutputParser
from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from pydantic import BaseModel

from app.llm.base import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError, schema_retry_message, template_refill_message,
    validate_structured_output,
)


class StructuredOutputParser(BaseOutputParser):
    """Preserve the application's repair rules and public error classification."""

    schema_type: type[BaseModel]

    def parse(self, text: str) -> BaseModel:
        return validate_structured_output(text, self.schema_type)

    @property
    def _type(self) -> str:
        return "sre_structured_output"


@dataclass
class StructuredResult:
    value: BaseModel | None = None
    original_output: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retry_count: int = 0


async def generate_structured(
    llm: LLM, messages: list[LLMMessage], schema: type[BaseModel], *,
    retries: int, template: dict[str, Any],
    empty_response: str = "{}", first_nonempty: bool = True,
) -> StructuredResult:
    """Normal attempts followed by exactly one template refill, with no hidden retry."""
    result = StructuredResult()
    parser = StructuredOutputParser(schema_type=schema)
    # Stubs and custom domain LLMs remain usable without inheriting framework types.
    model = RunnableLambda(llm.complete)
    messages = list(messages)
    with tracing_context(enabled=False):
        for attempt in range(retries + 2):
            refill = attempt == retries + 1
            if refill:
                result.retry_count += 1
                messages.append(LLMMessage("user", template_refill_message(template, result.original_output)))
            response = await model.ainvoke(messages)
            result.prompt_tokens += response.prompt_tokens
            result.completion_tokens += response.completion_tokens
            if not refill:
                if attempt == 0 or (first_nonempty and not result.original_output):
                    result.original_output = response.content
                messages.append(LLMMessage("assistant", response.content or empty_response))
            try:
                result.value = parser.invoke(response.content)
                return result
            except StructuredOutputError as exc:
                if attempt < retries:
                    result.retry_count += 1
                    messages.append(LLMMessage("user", schema_retry_message(exc)))
    return result
