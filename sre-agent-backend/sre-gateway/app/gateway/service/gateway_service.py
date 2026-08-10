"""串联 Parser、Model Router、Provider Adapter 和 Usage/Logs。"""

import time
import uuid
from datetime import UTC, datetime

from app.gateway.model_router import ModelRouter
from app.gateway.protocol import ProtocolParser
from app.gateway.provider import (
    BaseProviderAdapter,
    ProviderConfigurationError,
    ProviderRequestError,
)
from app.gateway.repository import UsageLogEntry, UsageLogRepository
from app.gateway.schema import (
    AssistantMessage,
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatUsage,
)


class GatewayService:
    """执行一次完整 LLM Gateway 调用流程。"""

    def __init__(
        self,
        parser: ProtocolParser,
        model_router: ModelRouter,
        providers: dict[str, BaseProviderAdapter],
        usage_repository: UsageLogRepository,
    ) -> None:
        self.parser = parser
        self.model_router = model_router
        self.providers = providers
        self.usage_repository = usage_repository

    async def complete(
        self, request: ChatCompletionRequest, client_api_key_id: int
    ) -> ChatCompletionResponse:
        """完成解析、路由、Provider 调用、日志记录和统一响应转换。

        ``client_api_key_id`` 仅用于标识谁调用了 Gateway；Adapter 自己从服务端
        配置读取 Provider API Key，二者不会互相传递或替代。
        """
        normalized = self.parser.parse(request)
        route = self.model_router.route(normalized.model)
        adapter = self.providers[route.provider]
        request_id = f"gw-{uuid.uuid4().hex}"
        started = time.perf_counter()

        try:
            result = await adapter.complete(normalized, route.model)
        except ProviderConfigurationError as exc:
            latency_ms = _elapsed_ms(started)
            self._log(request_id, client_api_key_id, route.provider, route.model, latency_ms, False, 503, str(exc))
            raise
        except ProviderRequestError as exc:
            latency_ms = _elapsed_ms(started)
            self._log(request_id, client_api_key_id, route.provider, route.model, latency_ms, False, exc.status_code, str(exc))
            raise

        latency_ms = _elapsed_ms(started)
        self._log(
            request_id,
            client_api_key_id,
            route.provider,
            result.model,
            latency_ms,
            True,
            200,
            None,
            result.prompt_tokens,
            result.completion_tokens,
        )
        return ChatCompletionResponse(
            id=result.response_id,
            created=int(time.time()),
            model=result.model,
            provider=route.provider,
            choices=[
                ChatChoice(
                    message=AssistantMessage(content=result.content),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=ChatUsage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.prompt_tokens + result.completion_tokens,
            ),
            latency_ms=latency_ms,
        )

    def _log(
        self,
        request_id: str,
        client_api_key_id: int,
        provider: str,
        model: str,
        latency_ms: int,
        success: bool,
        status_code: int,
        error_message: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """组装不含对话内容的 Usage 日志并交给 Repository。"""
        self.usage_repository.create(
            UsageLogEntry(
                request_id=request_id,
                client_api_key_id=client_api_key_id,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                success=success,
                status_code=status_code,
                error_message=error_message,
                created_at=datetime.now(UTC).isoformat(),
            )
        )


def _elapsed_ms(started: float) -> int:
    """计算单调时钟耗时，最小记录为 0 毫秒。"""
    return max(0, round((time.perf_counter() - started) * 1000))
