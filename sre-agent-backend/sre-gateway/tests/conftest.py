"""Gateway 测试使用 Docker MySQL 中的独立数据库。"""

import os

import pytest

from app.core.config import mysql_settings
from app.core.database import Database


@pytest.fixture
def gateway_database() -> Database:
    settings = mysql_settings()
    database = Database(
        settings.host,
        settings.port,
        settings.user,
        settings.password,
        os.getenv("GATEWAY_MYSQL_TEST_DATABASE", "sre_gateway_test"),
    )
    _clear_tables(database)
    yield database
    _clear_tables(database)
    database.dispose()


def _clear_tables(database: Database) -> None:
    with database.engine.begin() as connection:
        for table in ("gateway_usage_logs", "gateway_operation_logs", "gateway_tokens"):
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table}`")
