"""Bearer Token 的 FastAPI 鉴权依赖。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.service import TokenPrincipal, TokenService

# 禁止安全组件自动返回错误，由本层将所有鉴权失败统一处理成 401。
bearer_scheme = HTTPBearer(auto_error=False)


def get_token_service(request: Request) -> TokenService:
    """从 FastAPI 应用状态中取得生命周期内共享的 Service。"""
    return request.app.state.token_service


def require_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    service: Annotated[TokenService, Depends(get_token_service)],
) -> TokenPrincipal:
    """校验 Bearer Token，失败时统一返回 401 Unauthorized。"""
    principal = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        principal = service.validate(credentials.credentials)

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal

