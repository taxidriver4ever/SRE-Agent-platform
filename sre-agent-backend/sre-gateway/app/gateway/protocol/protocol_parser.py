"""统一请求协议解析器。"""

from app.gateway.schema import ChatCompletionRequest


class ProtocolParser:
    """解释客户端请求并输出稳定、与厂商无关的请求对象。

    Pydantic 已完成字段类型与范围校验；Parser 负责协议层规范化，并为未来支持
    OpenAI Responses、Anthropic Messages 等客户端协议预留单一入口。
    """

    def parse(self, request: ChatCompletionRequest) -> ChatCompletionRequest:
        """返回独立副本，防止后续 Adapter 修改客户端原始请求。"""
        return request.model_copy(deep=True)

