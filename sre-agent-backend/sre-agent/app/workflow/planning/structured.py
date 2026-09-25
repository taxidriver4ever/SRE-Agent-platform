"""Planner 与 Synthesis 共用的 Structured Output 重试。"""

import json
from typing import Any

from pydantic import BaseModel

from app.llm import LLM
from app.llm.prompts import prompt_messages
from app.llm.structured import generate_structured


async def complete_structured(
    llm: LLM,
    retries: int,
    system: str,
    payload: dict[str, Any],
    schema: type[BaseModel],
    template: dict[str, Any],
) -> tuple[Any, int, int, int]:
    """返回校验结果、Token 用量和实际触发的结构化输出重试次数。"""
    result = await generate_structured(
        llm, prompt_messages(system, json.dumps(payload, ensure_ascii=False, default=str)),
        schema, retries=retries, template=template,
    )
    value = result.value if result.value is not None else schema.model_validate(template)
    return value, result.prompt_tokens, result.completion_tokens, result.retry_count
