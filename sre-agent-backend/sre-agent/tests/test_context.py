"""Active Context Compaction、MinIO Evidence Store 和 Source Reference 回归测试。"""

from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.context import ActiveContextCompactor, EvidenceStore, SourceReference
from app.core.database import ApplicationDatabase
from app.storage import ObjectMetadata
from app.workflow.models import Evidence


class FakeObjectStore:
    """只用于单元测试的内存对象存储；生产代码始终使用 Docker MinIO。"""

    bucket = "test-evidence"

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def put_json(self, oss_key: str, payload: dict) -> None:
        """模拟 MinIO JSON 写入，并用深拷贝防止测试误改原对象。"""
        self.objects[oss_key] = copy.deepcopy(payload)

    def get_json(self, oss_key: str) -> dict:
        """模拟按 Key 回读完整 Evidence JSON。"""
        return copy.deepcopy(self.objects[oss_key])

    def stat(self, oss_key: str) -> ObjectMetadata:
        """本测试只登记 JSON Evidence，因此提供确定的对象元数据。"""
        payload = str(self.objects[oss_key]).encode("utf-8")
        return ObjectMetadata(oss_key=oss_key, size=len(payload), content_type="application/json")

    def get_bytes(self, oss_key: str, max_bytes: int | None = None) -> tuple[bytes, bool]:
        """补足 EvidenceStore 协议；当前两个测试不会走用户上传分支。"""
        payload = str(self.objects[oss_key]).encode("utf-8")
        if max_bytes is not None and len(payload) > max_bytes:
            return payload[:max_bytes], True
        return payload, False


def make_evidence(index: int, source: str, detail: str) -> Evidence:
    """生成具有完整引用的测试证据，避免测试依赖真实外部系统。"""
    return Evidence(
        source=source,
        tool_name=f"tool_{index}",
        title=f"证据 {index}",
        detail=detail,
        timestamp=datetime.now(timezone.utc),
        evidence_id=f"ev_{index}",
        source_references=[SourceReference(
            kind="logs",
            uri=f"loki://service/test-{index}",
            label=f"test-{index}",
        )],
    )


def _database_path() -> Path:
    """Windows 沙箱不开放系统 TEMP，所以在被 gitignore 的 .data 下创建唯一测试库。"""
    return Path(".data") / f"test-evidence-{uuid4().hex}.sqlite3"


def test_evidence_store_keeps_full_result_while_preview_is_compacted():
    """上下文摘要被裁剪后，MinIO 对象仍必须保留完整原始字符串。"""
    database_path = _database_path()
    try:
        store = EvidenceStore(ApplicationDatabase(str(database_path)), FakeObjectStore())
        raw = {"logs": "x" * 5000}
        evidence_id = store.put("run-1", "query_logs", {"service": "order-service"}, raw, [])
        preview = ActiveContextCompactor().compact_result(raw, limit=120)

        assert len(preview) < len(raw["logs"])
        assert store.get("run-1", evidence_id)["result"] == raw
    finally:
        database_path.unlink(missing_ok=True)


def test_evidence_mapping_survives_recreation_without_raw_payload_in_database():
    """新 Store 可凭 oss_key 回查 MinIO，且 SQLite 表中不存在正文列。"""
    database_path = _database_path()
    object_store = FakeObjectStore()
    try:
        database = ApplicationDatabase(str(database_path))
        first = EvidenceStore(database, object_store)
        evidence_id = first.put("run-persisted", "query_trace", {}, {"trace_id": "abc"}, [])
        reopened = EvidenceStore(ApplicationDatabase(str(database_path)), object_store)

        assert reopened.get("run-persisted", evidence_id)["result"] == {"trace_id": "abc"}
        # sqlite3.Connection 的 context manager 只管理事务；closing 才会在 Windows
        # 及时释放句柄，确保 finally 能删除隔离测试库。
        with closing(reopened.database.connect()) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(evidence_objects)")]
        assert columns == ["run_id", "evidence_id", "oss_key", "created_at"]
    finally:
        database_path.unlink(missing_ok=True)


def test_active_compaction_preserves_source_diversity_and_ids():
    """同类日志很多时仍要保留 Trace/Database，并携带可回查 evidence_id。"""
    evidence = [
        make_evidence(1, "Loki", "日志一"),
        make_evidence(2, "Loki", "日志二"),
        make_evidence(3, "Tempo", "慢 Span"),
        make_evidence(4, "MySQL", "全表扫描"),
    ]
    context = ActiveContextCompactor(character_budget=2000, item_budget=3).build_active_context(evidence)

    assert "[ev_3] [Tempo]" in context
    assert "[ev_4] [MySQL]" in context
    assert "loki://" in context
