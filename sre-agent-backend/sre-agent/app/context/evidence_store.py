"""以 MinIO 保存原始证据、以 SQLite 保存 ``oss_key`` 映射的 Evidence Store。"""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from app.context.sources import SourceReference
from app.core.database import ApplicationDatabase
from app.storage import MinioObjectStore


class EvidenceStore:
    """完整 Evidence 写入 MinIO，应用数据库只保留定位对象所需的 Key。"""

    # 浏览器查看用户上传的日志时最多返回 2 MiB 预览，防止一次 API 回读巨大文件。
    RAW_PREVIEW_BYTES = 2 * 1024 * 1024
    _TEXT_SUFFIXES = {".txt", ".log", ".json", ".yaml", ".yml", ".xml", ".csv", ".md", ".sql"}

    def __init__(self, database: ApplicationDatabase, object_store: MinioObjectStore) -> None:
        self.database = database
        self.object_store = object_store

    def put(
        self,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        references: list[SourceReference],
    ) -> str:
        """保存完整 Tool 结果到 MinIO，并返回内容稳定的 ``evidence_id``。"""
        digest_source = f"{run_id}:{tool_name}:{result!s}".encode("utf-8", errors="replace")
        evidence_id = f"ev_{hashlib.sha256(digest_source).hexdigest()[:16]}"
        stored_at = datetime.now(timezone.utc).isoformat()
        oss_key = f"evidence/{run_id}/{evidence_id}.json"
        item = {
            "evidence_id": evidence_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "source_references": [reference.model_dump(mode="json") for reference in references],
            "stored_at": stored_at,
            "oss_key": oss_key,
        }
        # 先写对象、后写映射。这样数据库永远不会指向尚未存在的 MinIO 对象；
        # 极少数数据库写入失败只会形成可清理的孤立对象，不会产生损坏引用。
        self.object_store.put_json(oss_key, item)
        self._upsert_mapping(run_id, evidence_id, oss_key, stored_at)
        return evidence_id

    def register_uploaded(
        self,
        run_id: str,
        oss_key: str,
    ) -> tuple[str, dict[str, Any]]:
        """把已完成的用户直传对象登记为本次诊断的 Evidence，不复制对象正文。"""
        evidence_id = f"ev_{hashlib.sha256(f'{run_id}:{oss_key}'.encode()).hexdigest()[:16]}"
        stored_at = datetime.now(timezone.utc).isoformat()
        self._upsert_mapping(run_id, evidence_id, oss_key, stored_at)
        item = self._read_uploaded(evidence_id, oss_key, stored_at)
        return evidence_id, item

    def get(self, run_id: str, evidence_id: str) -> dict[str, Any] | None:
        """根据数据库中的唯一 ``oss_key`` 从 MinIO 回查完整 Evidence。"""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT oss_key, created_at FROM evidence_objects WHERE run_id = ? AND evidence_id = ?",
                (run_id, evidence_id),
            ).fetchone()
        if row is None:
            return None
        oss_key = str(row["oss_key"])
        if oss_key.startswith("evidence/"):
            return self.object_store.get_json(oss_key)
        return self._read_uploaded(evidence_id, oss_key, str(row["created_at"]))

    def list_run(self, run_id: str) -> list[dict[str, Any]]:
        """返回本次诊断的轻量对象索引；不会为了计数批量下载原始证据。"""
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, oss_key, created_at
                FROM evidence_objects
                WHERE run_id = ?
                ORDER BY created_at, evidence_id
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _read_uploaded(self, evidence_id: str, oss_key: str, stored_at: str) -> dict[str, Any]:
        """为文本对象提供有界预览；二进制对象只返回安全元数据。"""
        metadata = self.object_store.stat(oss_key)
        suffix = PurePosixPath(oss_key).suffix.lower()
        is_text = metadata.content_type.startswith("text/") or suffix in self._TEXT_SUFFIXES
        result: dict[str, Any] = {
            "oss_key": oss_key,
            "size": metadata.size,
            "content_type": metadata.content_type,
        }
        if is_text:
            raw, truncated = self.object_store.get_bytes(oss_key, self.RAW_PREVIEW_BYTES)
            result["text"] = raw.decode("utf-8", errors="replace")
            result["truncated"] = truncated
        return {
            "evidence_id": evidence_id,
            "tool_name": "minio_uploaded_evidence",
            "arguments": {"oss_key": oss_key},
            "result": result,
            "source_references": [{
                "kind": "object_storage",
                "uri": f"minio://{self.object_store.bucket}/{oss_key}",
                "label": PurePosixPath(oss_key).name,
            }],
            "stored_at": stored_at,
            "oss_key": oss_key,
        }

    def _upsert_mapping(
        self, run_id: str, evidence_id: str, oss_key: str, created_at: str
    ) -> None:
        """只写入对象 Key 与定位字段，禁止把序列化正文带入 SQL。"""
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO evidence_objects(run_id, evidence_id, oss_key, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, evidence_id) DO UPDATE SET
                    oss_key = excluded.oss_key,
                    created_at = excluded.created_at
                """,
                (run_id, evidence_id, oss_key, created_at),
            )
            connection.commit()
