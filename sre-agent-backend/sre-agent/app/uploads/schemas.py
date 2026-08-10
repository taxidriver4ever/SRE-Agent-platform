"""浏览器直传 MinIO 所需的请求与响应模型。"""

from typing import Literal

from pydantic import BaseModel, Field


class PresignUploadRequest(BaseModel):
    """申请一个绑定当前用户和会话的短时效 PUT 地址。"""

    conversation_id: str = Field(min_length=32, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", min_length=1, max_length=200)
    size: int = Field(ge=1)
    # pasted_log 与普通文件使用不同 Key 前缀，便于生命周期策略和审计识别。
    kind: Literal["file", "pasted_log"] = "file"


class PresignUploadResponse(BaseModel):
    """预签名仅返回浏览器内存，不应被客户端写入 localStorage。"""

    oss_key: str
    upload_url: str
    expires_at: str


class CompleteUploadRequest(BaseModel):
    """浏览器 PUT 成功后通知后端校验并写入会话对象映射。"""

    conversation_id: str = Field(min_length=32, max_length=64)
    oss_key: str = Field(min_length=1, max_length=1024)
    expected_size: int = Field(ge=1)


class UploadObjectResponse(BaseModel):
    """数据库与前端长期保存的只有稳定 ``oss_key``，没有签名 URL。"""

    oss_key: str
    size: int
    content_type: str


class DownloadUrlResponse(BaseModel):
    """按需生成的短时效下载地址。"""

    oss_key: str
    download_url: str
    expires_at: str
