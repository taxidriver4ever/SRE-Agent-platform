"""与具体模型服务解耦的 LLM 领域接口。

Agent 只依赖本模块声明的数据对象和 ``LLM`` 协议，不直接依赖 HTTP、网关或
某个模型厂商 SDK。这种边界使更换网关协议和编写无网络单元测试更简单。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """一条与厂商无关的对话消息。

    ``role`` 当前使用网关支持的 system、user、assistant 三种值。这里不限定
    Literal，是为了以后扩展原生 tool role 时不必破坏领域接口。
    """

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """LLM 单轮响应及网关提供的计量信息。"""

    # 模型生成的原始文本，Agent 会在自己的协议层进行 JSON 验证。
    content: str
    # 网关返回的实际模型名；可能与请求中的路由别名不同。
    model: str
    # 实际处理请求的 Provider。自定义 LLM 实现可以不提供。
    provider: str | None = None
    # 输入/输出 Token 分开保存，便于分别计算成本和观察上下文增长。
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLM(Protocol):
    """所有 Agent 可用 LLM 客户端都应满足的结构化协议。

    ``Protocol`` 使用结构化子类型：实现类无需显式继承，只要提供同签名的
    ``complete`` 方法即可被 ToolAgent 使用。
    """

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        """根据完整消息上下文异步生成下一条 assistant 消息。"""
