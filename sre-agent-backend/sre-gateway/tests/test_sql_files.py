"""验证 Gateway 各数据模块使用带 COMMENT 的独立 SQL 文件。"""

from pathlib import Path


def test_gateway_module_sql_files_are_present_and_documented():
    gateway_root = Path(__file__).resolve().parents[1]
    sql_files = [
        gateway_root / "app" / "auth" / "sql" / "schema.sql",
        gateway_root / "app" / "gateway" / "sql" / "schema.sql",
        gateway_root / "app" / "operation_log" / "sql" / "schema.sql",
    ]

    for sql_file in sql_files:
        sql_text = sql_file.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in sql_text
        assert "COMMENT:" in sql_text
