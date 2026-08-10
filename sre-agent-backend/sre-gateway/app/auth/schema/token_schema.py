"""Token HTTP API 使用的 Pydantic Schema。"""

from pydantic import BaseModel


class GeneratedTokenResponse(BaseModel):
    """生成 Token 接口响应；明文 Token 只在本次响应出现。"""

    token: str
    created_at: str

