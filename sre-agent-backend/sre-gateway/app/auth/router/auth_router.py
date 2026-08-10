"""Auth 模块的 HTTP 路由，只负责协议转换和响应组织。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.auth.dependency import get_token_service, require_token
from app.auth.schema import GeneratedTokenResponse
from app.auth.service import TokenPrincipal, TokenService

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post(
    "/tokens",
    response_model=GeneratedTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_token(request: Request) -> GeneratedTokenResponse:
    """调用 Service 生成 Token，并把结果转换为 HTTP Response Schema。"""
    service: TokenService = get_token_service(request)
    generated = service.generate()
    return GeneratedTokenResponse(
        token=generated.token,
        created_at=generated.created_at,
    )


@router.get("/check")
def check_auth(
    principal: Annotated[TokenPrincipal, Depends(require_token)],
) -> dict[str, int | bool]:
    """Bearer Token 校验成功后返回最小身份结果。"""
    return {"authenticated": True, "token_id": principal.token_id}

