"""验证 Gateway Pydantic Schema 的字段文档完整性。"""

from app.gateway.schema import (
    AssistantMessage,
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
)


def test_all_gateway_schema_fields_have_descriptions():
    """所有字段必须提供 description，确保 Swagger 能解释每个属性。"""
    schema_classes = [
        ChatMessage,
        AssistantMessage,
        ChatCompletionRequest,
        ChatUsage,
        ChatChoice,
        ChatCompletionResponse,
    ]

    for schema_class in schema_classes:
        for field_name, field in schema_class.model_fields.items():
            assert field.description, (
                f"{schema_class.__name__}.{field_name} 缺少 Field description"
            )
