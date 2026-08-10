"""MinIO 对象读写与预签名 URL 封装。

该模块刻意不包含用户权限或 Conversation 规则：它只负责 bucket、object key 和
字节流。业务层必须在调用前完成所有权校验，避免基础设施层逐渐混入业务逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Any

from minio import Minio


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """对象的最小只读元数据，不包含对象正文。"""

    oss_key: str
    size: int
    content_type: str


class MinioObjectStore:
    """使用私有 bucket 保存 Evidence，并生成短时效浏览器直传地址。"""

    def __init__(
        self,
        *,
        endpoint: str,
        public_endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        # internal client 真正访问 MinIO；public client 只负责使用浏览器可访问的
        # Host 生成 SigV4 URL。二者必须使用同一凭证与 bucket。
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._public_client = Minio(
            public_endpoint, access_key=access_key, secret_key=secret_key, secure=secure
        )
        self.bucket = bucket
        self._bucket_ready = False

    def ensure_bucket(self) -> None:
        """惰性创建私有 bucket，让健康检查不被暂时离线的 MinIO 拖垮。"""
        if self._bucket_ready:
            return
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)
        self._bucket_ready = True

    def put_json(self, oss_key: str, payload: Any) -> None:
        """把完整 Tool 结果序列化为 UTF-8 JSON 后写入 MinIO。"""
        self._validate_key(oss_key)
        self.ensure_bucket()
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._client.put_object(
            self.bucket,
            oss_key,
            BytesIO(body),
            length=len(body),
            content_type="application/json; charset=utf-8",
        )

    def get_json(self, oss_key: str) -> dict[str, Any]:
        """读取项目生成的 Evidence JSON，并确保响应连接被及时归还。"""
        raw, _ = self.get_bytes(oss_key)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("evidence object must contain a JSON object")
        return value

    def get_bytes(self, oss_key: str, max_bytes: int | None = None) -> tuple[bytes, bool]:
        """有界读取对象；返回的布尔值表示正文是否因为上限而被裁剪。"""
        self._validate_key(oss_key)
        self.ensure_bucket()
        response = self._client.get_object(self.bucket, oss_key)
        try:
            # 多读 1 字节用于精确判断是否截断，避免把恰好等于上限的文件误标记。
            limit = max_bytes + 1 if max_bytes is not None else None
            body = response.read(limit)
        finally:
            response.close()
            response.release_conn()
        if max_bytes is not None and len(body) > max_bytes:
            return body[:max_bytes], True
        return body, False

    def stat(self, oss_key: str) -> ObjectMetadata:
        """读取上传完成校验所需的大小与 Content-Type，不下载文件正文。"""
        self._validate_key(oss_key)
        self.ensure_bucket()
        item = self._client.stat_object(self.bucket, oss_key)
        return ObjectMetadata(
            oss_key=oss_key,
            size=int(item.size or 0),
            content_type=str(item.content_type or "application/octet-stream"),
        )

    def presigned_put(self, oss_key: str, expire_minutes: int) -> str:
        """生成仅允许向指定 Key 执行 PUT 的短时效 URL。"""
        self._validate_key(oss_key)
        self.ensure_bucket()
        return self._public_client.presigned_put_object(
            self.bucket, oss_key, expires=timedelta(minutes=expire_minutes)
        )

    def presigned_get(self, oss_key: str, expire_minutes: int) -> str:
        """生成短时效下载地址；bucket 本身始终保持私有。"""
        self._validate_key(oss_key)
        self.ensure_bucket()
        return self._public_client.presigned_get_object(
            self.bucket, oss_key, expires=timedelta(minutes=expire_minutes)
        )

    @staticmethod
    def _validate_key(oss_key: str) -> None:
        """拒绝空 Key、反斜杠和路径穿越片段，统一使用 S3 风格正斜杠。"""
        if not oss_key or "\\" in oss_key or any(part in {"", ".", ".."} for part in oss_key.split("/")):
            raise ValueError("invalid oss key")
