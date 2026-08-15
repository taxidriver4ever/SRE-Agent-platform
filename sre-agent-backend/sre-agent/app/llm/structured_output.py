"""模型结构化输出的 JSON 修复、错误分类与 Schema 校验。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredOutputErrorKind(str, Enum):
    """决定下一步应本地修复还是把校验错误反馈给模型。"""

    JSON_FORMAT = "json_format"
    SCHEMA_VALIDATION = "schema_validation"


@dataclass(slots=True)
class StructuredOutputError(Exception):
    """携带安全、可回传模型的结构化输出错误。"""

    kind: StructuredOutputErrorKind
    detail: str
    raw_output: str

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.detail}"


def validate_structured_output(content: str, schema: type[ModelT]) -> ModelT:
    """解析 JSON、执行有限修复，然后用 Pydantic Schema 做最终校验。"""
    try:
        value = json.loads(_strip_fence(content))
    except (json.JSONDecodeError, ValueError):
        try:
            value = json.loads(repair_json(content))
        except (json.JSONDecodeError, ValueError) as exc:
            raise StructuredOutputError(
                StructuredOutputErrorKind.JSON_FORMAT,
                str(exc)[:500],
                content,
            ) from exc

    if not isinstance(value, dict):
        raise StructuredOutputError(
            StructuredOutputErrorKind.SCHEMA_VALIDATION,
            "top-level value must be a JSON object",
            content,
        )
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        raise StructuredOutputError(
            StructuredOutputErrorKind.SCHEMA_VALIDATION,
            str(exc)[:1000],
            content,
        ) from exc


def repair_json(content: str) -> str:
    """保守修复常见格式问题；不猜测缺失的业务字段或字段类型。"""
    text = _strip_fence(content)
    candidate = _first_balanced_object(text)
    if candidate is None:
        raise ValueError("response does not contain a complete JSON object")
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        # Python 字面量解析只用于兼容单引号、True/False/None，且不会执行代码。
        try:
            value = ast.literal_eval(candidate)
        except (SyntaxError, ValueError) as exc:
            raise ValueError("JSON repair could not recover a valid object") from exc
        if not isinstance(value, dict):
            raise ValueError("repaired value is not an object")
        return json.dumps(value, ensure_ascii=False)


def schema_retry_message(error: StructuredOutputError) -> str:
    """按错误类型生成定向反馈，避免模型在重试时盲目改写。"""
    action = (
        "JSON Repair 后仍无法解析，请修正括号、引号、逗号等 JSON 格式"
        if error.kind is StructuredOutputErrorKind.JSON_FORMAT
        else "JSON 已可解析，但字段缺失或类型不符合 Schema，请按校验错误修正字段"
    )
    return json.dumps(
        {
            "type": "protocol_error",
            "error_kind": error.kind.value,
            "message": action,
            "validation_error": error.detail[:500],
            "instruction": "只返回修正后的一个 JSON 对象，不要附加解释。",
        },
        ensure_ascii=False,
    )


def template_refill_message(template: dict[str, Any], original_output: str) -> str:
    """在常规重试耗尽后要求模型按预设模板重新填充原始结果。"""
    return json.dumps(
        {
            "type": "structured_output_template_refill",
            "instruction": "常规修复已耗尽。严格保留模板字段和字段类型，根据原始结果重新填充；只输出 JSON。",
            "template": template,
            "original_output": original_output[:8000],
        },
        ensure_ascii=False,
        default=str,
    )


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text


def _first_balanced_object(text: str) -> str | None:
    start = -1
    depth = 0
    in_string = False
    escaped = False
    quote = '"'
    for index, character in enumerate(text):
        if start < 0:
            if character == "{":
                start, depth = index, 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                in_string = False
            continue
        if character in ('"', "'"):
            in_string, quote = True, character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None
