"""Audit Log 模块独立维护自己的表结构。"""

from pathlib import Path

from app.core.database import ApplicationDatabase


SCHEMA_FILE = Path(__file__).resolve().parent / "sql" / "schema.sql"


def initialize_audit_schema(database: ApplicationDatabase) -> None:
    database.initialize_schema_file(SCHEMA_FILE)
