"""Agent 业务数据的 MySQL 连接基础设施。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


class DatabaseRow(dict[str, Any]):
    """同时支持 ``row["column"]`` 与旧代码 ``row[0]`` 的查询结果。"""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class QueryResult:
    """将 PyMySQL Cursor 收敛成业务层使用的轻量结果接口。"""

    def __init__(self, cursor: Any | None) -> None:
        self._cursor = cursor

    def fetchone(self) -> DatabaseRow | None:
        if self._cursor is None:
            return None
        try:
            value = self._cursor.fetchone()
            return DatabaseRow(value) if value is not None else None
        finally:
            self._cursor.close()
            self._cursor = None

    def fetchall(self) -> list[DatabaseRow]:
        if self._cursor is None:
            return []
        try:
            return [DatabaseRow(row) for row in self._cursor.fetchall()]
        finally:
            self._cursor.close()
            self._cursor = None

    def __iter__(self) -> Iterator[DatabaseRow]:
        return iter(self.fetchall())


class DatabaseConnection:
    """提供业务 Repository 已使用的 execute/commit 短连接接口。"""

    def __init__(self, raw_connection: Any) -> None:
        self._raw_connection = raw_connection

    def __enter__(self) -> "DatabaseConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()

    def execute(self, sql: str, parameters: Sequence[Any] | None = None) -> QueryResult:
        cursor = self._raw_connection.cursor()
        try:
            cursor.execute(sql.replace("?", "%s"), tuple(parameters or ()))
        except Exception:
            cursor.close()
            raise
        if cursor.description is None:
            cursor.close()
            return QueryResult(None)
        return QueryResult(cursor)

    def commit(self) -> None:
        self._raw_connection.commit()

    def rollback(self) -> None:
        self._raw_connection.rollback()

    def close(self) -> None:
        self._raw_connection.close()


class ApplicationDatabase:
    """创建 MySQL 短连接，并执行各业务模块自行声明的建表语句。"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def connect(self) -> DatabaseConnection:
        return DatabaseConnection(pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        ))

    def initialize_schema(self, statements: Iterable[str]) -> None:
        """在一个事务中执行某个业务模块提供的幂等 MySQL DDL。"""
        connection = self.connect()
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema_file(self, sql_file: str | Path) -> None:
        """读取模块自己的 SQL 文件并执行其中以分号分隔的 DDL。"""
        sql_text = Path(sql_file).read_text(encoding="utf-8")
        statements = tuple(
            statement.strip()
            for statement in sql_text.split(";")
            if statement.strip()
        )
        self.initialize_schema(statements)
