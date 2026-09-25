"""LangChain model boundary; the existing Gateway remains the HTTP transport."""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ChatMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from pydantic import Field
from langsmith import tracing_context

from app.llm.base import LLM, LLMMessage, LLMResponse


def to_messages(messages: list[LLMMessage]) -> list[BaseMessage]:
    """Do not reinterpret braces, trim whitespace or promote observations to tool role."""
    classes = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [
        classes[item.role](content=item.content) if item.role in classes
        else ChatMessage(role=item.role, content=item.content)
        for item in messages
    ]


def from_messages(messages: list[BaseMessage]) -> list[LLMMessage]:
    roles = {"system": "system", "human": "user", "ai": "assistant"}
    result = []
    for item in messages:
        if not isinstance(item.content, str):
            raise ValueError("Gateway supports text messages only")
        role = item.role if isinstance(item, ChatMessage) else roles.get(item.type)
        if role is None:
            raise ValueError(f"Gateway does not support message type: {item.type}")
        result.append(LLMMessage(role, item.content))
    return result


class GatewayChatModel(BaseChatModel):
    """Async model abstraction without SDK retries, caching or transport changes."""

    transport: Any = Field(exclude=True, repr=False)

    @property
    def _llm_type(self) -> str:
        return "sre-gateway"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("SRE Gateway is asynchronous; use ainvoke")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop or kwargs:
            raise ValueError("Gateway generation parameters are configured on the transport")
        response = await self.transport.complete(from_messages(messages))
        message = AIMessage(
            content=response.content,
            response_metadata={"model": response.model, "provider": response.provider},
            usage_metadata={
                "input_tokens": response.prompt_tokens,
                "output_tokens": response.completion_tokens,
                "total_tokens": response.prompt_tokens + response.completion_tokens,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class LangChainLLM:
    """Compatibility facade shared by every existing LLM consumer."""

    def __init__(self, transport: LLM) -> None:
        self.transport = transport
        self.chat_model = GatewayChatModel(transport=transport, cache=False)
        self.chain = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("messages"),
        ]) | self.chat_model

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        from app.agent.observability import call_config
        # Diagnostics may contain private logs/source. Never opt in to remote tracing
        # merely because an unrelated shell has LANGSMITH_TRACING configured.
        with tracing_context(enabled=False):
            response = await self.chain.ainvoke({"messages": to_messages(messages)}, config=call_config())
        usage = response.usage_metadata or {}
        return LLMResponse(
            content=response.content,
            model=response.response_metadata["model"],
            provider=response.response_metadata.get("provider"),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
        )

    async def close(self) -> None:
        await self.transport.close()
