"""测试统一绑定到独立 MySQL 测试库，禁止接触生产业务库。"""

import pytest

from tests.mysql_support import mysql_test_database


@pytest.fixture(autouse=True)
def isolated_mysql_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLICATION_MYSQL_DATABASE", "sre_agent_test")
    mysql_test_database()
