"""Planner 与 Synthesis 共用的 Structured Output 重试。"""

import json
from typing import Any

from pydantic import BaseModel

from app.llm import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError, schema_retry_message, template_refill_message,
    validate_structured_output,
)


async def complete_structured(
    llm: LLM,
    retries: int,
    system: str,
    payload: dict[str, Any],
    schema: type[BaseModel],
    template: dict[str, Any],
) -> tuple[Any, int, int, int]:
    """返回校验结果、Token 用量和实际触发的结构化输出重试次数。"""
    messages = [
        LLMMessage("system", system),
        LLMMessage("user", json.dumps(payload, ensure_ascii=False, default=str)),
    ]
    prompt_tokens = 0
    completion_tokens = 0
    retry_count = 0
    first_output = ""
    for attempt in range(retries + 1):
        response = await llm.complete(messages)
        prompt_tokens += response.prompt_tokens
        completion_tokens += response.completion_tokens
        first_output = first_output or response.content
        messages.append(LLMMessage("assistant", response.content or "{}"))
        try:
            return validate_structured_output(response.content, schema), prompt_tokens, completion_tokens, retry_count
        except StructuredOutputError as exc:
            if attempt < retries:
                retry_count += 1
                messages.append(LLMMessage("user", schema_retry_message(exc)))
    retry_count += 1
    messages.append(LLMMessage("user", template_refill_message(template, first_output)))
    response = await llm.complete(messages)
    prompt_tokens += response.prompt_tokens
    completion_tokens += response.completion_tokens
    try:
        result = validate_structured_output(response.content, schema)
    except StructuredOutputError:
        result = schema.model_validate(template)
    return result, prompt_tokens, completion_tokens, retry_count
