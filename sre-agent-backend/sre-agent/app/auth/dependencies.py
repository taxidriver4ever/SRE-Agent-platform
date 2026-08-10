"""FastAPI Bearer Token 提取与当前用户依赖。"""

from typing import Annotated, TypedDict

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.service import AuthService


class CurrentUser(TypedDict):
    """路由内部使用的可信用户身份。"""

    id: str
    username: str


bearer = HTTPBearer(auto_error=False)


def get_auth_service(request: Request) -> AuthService:
    """从应用生命周期状态获取唯一 AuthService。"""
    return request.app.state.auth_service


def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> CurrentUser:
    """验证 Bearer Token；缺失、失效和过期统一返回 401。"""
    user = service.authenticate(credentials.credentials) if credentials else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return CurrentUser(id=user["id"], username=user["username"])
