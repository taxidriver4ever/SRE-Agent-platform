"""Auth 登录、身份恢复和注销 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependencies import CurrentUser, bearer, get_auth_service, require_user
from app.auth.schemas import LoginRequest, LoginResponse, UserResponse
from app.auth.service import AuthService


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, service: Annotated[AuthService, Depends(get_auth_service)]) -> LoginResponse:
    """用密码登录并返回一次可见的随机 Bearer Token。"""
    result = service.login(body.username, body.password)
    if result is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    return LoginResponse.model_validate(result)


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[CurrentUser, Depends(require_user)]) -> UserResponse:
    """前端启动时调用，用服务器校验替代单纯相信 localStorage。"""
    return UserResponse(**user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """撤销当前 Token；重复注销保持幂等。"""
    if credentials:
        service.logout(credentials.credentials)
