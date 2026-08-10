"""MinIO/S3 对象存储基础设施。"""

from app.storage.minio_store import MinioObjectStore, ObjectMetadata

__all__ = ["MinioObjectStore", "ObjectMetadata"]
