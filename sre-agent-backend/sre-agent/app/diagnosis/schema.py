"""Diagnosis 模块拥有的 MySQL 表结构。"""

from pathlib import Path

from app.core.database import ApplicationDatabase

SCHEMA_FILE = Path(__file__).resolve().parent / "sql" / "schema.sql"


def initialize_diagnosis_schema(database: ApplicationDatabase) -> None:
    database.initialize_schema_file(SCHEMA_FILE)
