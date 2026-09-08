"""Diagnosis 模块拥有的 MySQL 表结构。"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re

from app.core.database import ApplicationDatabase

SCHEMA_FILE = Path(__file__).resolve().parent / "sql" / "schema.sql"
MIGRATIONS = (
    ("001_durable_execution", Path(__file__).resolve().parent / "sql" / "001_durable_execution.sql"),
    ("002_tool_parent_evidence", Path(__file__).resolve().parent / "sql" / "002_tool_parent_evidence.sql"),
)


def _schema_objects(connection) -> tuple[set[str], set[str]]:
    columns = connection.execute(
        """
        SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN ('diagnosis_sessions', 'diagnosis_investigation_steps', 'diagnosis_events')
        """
    ).fetchall()
    indexes = connection.execute(
        """
        SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN ('diagnosis_sessions', 'diagnosis_investigation_steps', 'diagnosis_events')
        """
    ).fetchall()
    return (
        {f"{row['TABLE_NAME']}.{row['COLUMN_NAME']}" for row in columns},
        {f"{row['TABLE_NAME']}.{row['INDEX_NAME']}" for row in indexes},
    )


def _already_applied(statement: str, columns: set[str], indexes: set[str]) -> bool:
    """DDL 隐式提交后可从中断处安全重入，避免重复 ADD 导致启动失败。"""
    table_match = re.search(r"ALTER\s+TABLE\s+`?([a-zA-Z0-9_]+)`?", statement, re.I)
    if table_match is None:
        return False
    table = table_match.group(1)
    added_columns = re.findall(r"ADD\s+COLUMN\s+`?([a-zA-Z0-9_]+)`?", statement, re.I)
    added_indexes = re.findall(
        r"ADD\s+(?:UNIQUE\s+)?(?:INDEX|KEY)\s+`?([a-zA-Z0-9_]+)`?", statement, re.I,
    )
    objects = [*(f"{table}.{name}" in columns for name in added_columns),
               *(f"{table}.{name}" in indexes for name in added_indexes)]
    return bool(objects) and all(objects)


def initialize_diagnosis_schema(database: ApplicationDatabase) -> None:
    database.initialize_schema_file(SCHEMA_FILE)
    # MySQL named lock prevents multiple Uvicorn workers racing the same ALTER TABLE.
    with closing(database.connect()) as connection:
        acquired = connection.execute(
            "SELECT GET_LOCK('sre_agent_diagnosis_migrations', 30) AS acquired"
        ).fetchone()
        if not acquired or int(acquired["acquired"] or 0) != 1:
            raise RuntimeError("failed to acquire Diagnosis schema migration lock")
        try:
            for version, path in MIGRATIONS:
                applied = connection.execute(
                    "SELECT 1 FROM diagnosis_schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied:
                    continue
                sql_text = path.read_text(encoding="utf-8")
                statements = [item.strip() for item in sql_text.split(";") if item.strip()]
                columns, indexes = _schema_objects(connection)
                for statement in statements:
                    if _already_applied(statement, columns, indexes):
                        continue
                    connection.execute(statement)
                    columns, indexes = _schema_objects(connection)
                connection.execute(
                    "INSERT INTO diagnosis_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("SELECT RELEASE_LOCK('sre_agent_diagnosis_migrations')")
            connection.commit()
