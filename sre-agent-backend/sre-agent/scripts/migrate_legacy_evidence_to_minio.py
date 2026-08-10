"""把旧 SQLite ``payload_json`` Evidence 一次性迁移到 Docker MinIO。

默认只复制并校验，不修改旧库。显式传入 ``--purge-legacy`` 后，脚本仅在所有对象
写入、回读和新映射落库都成功时删除旧 ``evidence`` 表并执行 VACUUM。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# 直接执行 ``python scripts/...py`` 时 Python 只把 scripts/ 放进模块搜索路径。
# 显式加入项目根目录，保证脚本与 ``python -m`` 两种启动方式行为一致。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.database import ApplicationDatabase
from app.storage import MinioObjectStore


def migrate(legacy_path: Path, purge_legacy: bool) -> tuple[int, int]:
    """迁移并验证全部历史记录，返回 ``(读取数, 验证数)``。"""
    resolved = legacy_path.resolve()
    project_data = (Path.cwd() / ".data").resolve()
    # 清理属于不可逆操作；只允许处理项目 .data 下明确命名的旧库，拒绝任意路径。
    if resolved.parent != project_data or resolved.name != "evidence-store.sqlite3":
        raise ValueError("legacy database must be .data/evidence-store.sqlite3 in the project")
    if not resolved.exists():
        return 0, 0

    settings = get_settings()
    application_database = ApplicationDatabase(settings.application_database_path)
    object_store = MinioObjectStore(
        endpoint=settings.minio_endpoint,
        public_endpoint=settings.minio_public_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )

    with closing(sqlite3.connect(resolved)) as legacy:
        table = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence'"
        ).fetchone()
        if table is None:
            return 0, 0
        rows = legacy.execute(
            "SELECT run_id, evidence_id, stored_at, payload_json FROM evidence ORDER BY stored_at"
        ).fetchall()

        verified = 0
        for run_id, evidence_id, stored_at, payload_json in rows:
            payload = json.loads(payload_json)
            oss_key = f"evidence/{run_id}/{evidence_id}.json"
            payload["oss_key"] = oss_key
            object_store.put_json(oss_key, payload)
            # 每个对象必须能从 MinIO 回读到同一个 evidence_id，才允许登记映射。
            persisted = object_store.get_json(oss_key)
            if persisted.get("evidence_id") != evidence_id:
                raise RuntimeError(f"MinIO verification failed for {evidence_id}")
            with closing(application_database.connect()) as application:
                application.execute(
                    """
                    INSERT INTO evidence_objects(run_id, evidence_id, oss_key, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id, evidence_id) DO UPDATE SET
                        oss_key = excluded.oss_key,
                        created_at = excluded.created_at
                    """,
                    (run_id, evidence_id, oss_key, stored_at),
                )
                application.commit()
            verified += 1

        if purge_legacy and verified == len(rows):
            # DROP + VACUUM 真正移除历史 payload 页面；空旧文件可作为迁移已完成标记。
            legacy.execute("DROP TABLE evidence")
            legacy.commit()
            legacy.execute("VACUUM")
        return len(rows), verified


def main() -> None:
    """解析命令行参数并输出可供自动化核对的迁移计数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-path",
        type=Path,
        default=Path(".data/evidence-store.sqlite3"),
        help="旧 Evidence SQLite；出于清理安全只接受项目默认路径",
    )
    parser.add_argument(
        "--purge-legacy",
        action="store_true",
        help="全部对象验证成功后删除旧 evidence 表并 VACUUM",
    )
    args = parser.parse_args()
    read_count, verified_count = migrate(args.legacy_path, args.purge_legacy)
    print(f"legacy_read={read_count} minio_verified={verified_count} purged={args.purge_legacy}")


if __name__ == "__main__":
    main()
