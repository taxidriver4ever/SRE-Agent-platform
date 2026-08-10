"""LLM Gateway HTTP 入口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.dependency import require_token
from app.auth.service import TokenPrincipal
from app.gateway.provider import ProviderConfigurationError, ProviderRequestError
from app.gateway.schema import ChatCompletionRequest, ChatCompletionResponse
from app.gateway.service import GatewayService

router = APIRouter(prefix="/v1/gateway", tags=["gateway"])


def get_gateway_service(request: Request) -> GatewayService:
    """从应用状态中取得生命周期内共享的 GatewayService。"""
    return request.app.state.gateway_service


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request_body: ChatCompletionRequest,
    principal: Annotated[TokenPrincipal, Depends(require_token)],
    service: Annotated[GatewayService, Depends(get_gateway_service)],
) -> ChatCompletionResponse:
    """校验 Gateway Token 后执行完整模型调用链。"""
    try:
        # 只把用户 Key 对应的内部 ID 交给 Service 记账；用户 Key 明文不会进入
        # GatewayService，更不会被当作 Provider API Key 使用。
        return await service.complete(request_body, principal.token_id)
    except ProviderConfigurationError as exc:
        # 缺少服务端厂商密钥属于服务未配置，而不是客户端请求错误。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ProviderRequestError as exc:
        # 上游的 4xx/5xx 不原样透传，统一作为 Gateway 上游调用失败返回。
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
