"""密码登录、Token 生命周期和 FastAPI 身份依赖。"""

from app.auth.dependencies import CurrentUser, require_user
from app.auth.router import router as auth_router
from app.auth.service import AuthService

__all__ = ["AuthService", "CurrentUser", "auth_router", "require_user"]
