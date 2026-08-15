"""SQLAlchemy 数据库基础设施。

本模块只负责创建 Engine、Session 和执行模块 SQL 文件，不直接导入任何业务模型。
"""

from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型共享的声明式基类。"""


class Database:
    """封装 SQLite Engine 与 Session 工厂。

    Args:
        path: SQLite 数据库文件路径，可以是字符串或 ``Path``。

    一个 ``Database`` 实例可以在应用生命周期内复用；具体查询使用短生命周期
    Session，避免连接和事务长期占用。
    """

    def __init__(self, path: str | Path) -> None:
        """创建数据库配置，Engine 会在首次访问数据库时建立真实连接。"""
        self.path = Path(path)
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            # FastAPI 的同步依赖可能在线程池中运行，SQLite 默认的线程检查会
            # 阻止连接跨线程使用，因此在应用层显式关闭该检查。
            connect_args={"check_same_thread": False},
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            # 禁止隐式 flush，使写入发生时机更明确。
            autoflush=False,
            # commit 后对象属性仍可读取，适合本项目的短事务服务方法。
            expire_on_commit=False,
        )

    def execute_schema_file(self, sql_file: str | Path) -> None:
        """执行业务模块自己的 SQLite SQL 文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sql_text = Path(sql_file).read_text(encoding="utf-8")
        with sqlite3.connect(self.path) as connection:
            connection.executescript(sql_text)

    def session(self) -> Session:
        """创建一个新的 SQLAlchemy Session。

        调用方应使用 ``with database.session() as session``，确保查询结束后连接
        被归还连接池；写操作还需要显式调用 ``session.commit()``。
        """
        return self._session_factory()

    def dispose(self) -> None:
        """释放 Engine 维护的全部数据库连接，用于应用关闭阶段。"""
        self.engine.dispose()
