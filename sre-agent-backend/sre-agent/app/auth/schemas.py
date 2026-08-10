"""Auth HTTP 请求与响应的数据契约。"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """用户名和密码登录请求；密码不会进入日志或响应。"""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6, max_length=256)


class UserResponse(BaseModel):
    """允许返回给浏览器的最小用户信息。"""

    id: str
    username: str


class LoginResponse(BaseModel):
    """登录成功后返回一次明文 Token 及其到期时间。"""

    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserResponse
