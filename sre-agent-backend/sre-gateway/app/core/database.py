"""Gateway 共用的 MySQL SQLAlchemy 基础设施。"""

from pathlib import Path

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型共享的声明式基类。"""


class Database:
    """封装 MySQL Engine、Session 工厂和模块化 SQL 初始化。"""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
    ) -> None:
        self.database_name = database
        self.engine = create_engine(
            URL.create(
                "mysql+pymysql", username=user, password=password,
                host=host, port=port, database=database,
            ),
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            # 禁止隐式 flush，使写入发生时机更明确。
            autoflush=False,
            # commit 后对象属性仍可读取，适合本项目的短事务服务方法。
            expire_on_commit=False,
        )

    def execute_schema_file(self, sql_file: str | Path) -> None:
        """逐条执行业务模块自己的 MySQL SQL 文件。"""
        sql_text = Path(sql_file).read_text(encoding="utf-8")
        statements = [statement.strip() for statement in sql_text.split(";") if statement.strip()]
        with self.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    def session(self) -> Session:
        """创建一个新的 SQLAlchemy Session。

        调用方应使用 ``with database.session() as session``，确保查询结束后连接
        被归还连接池；写操作还需要显式调用 ``session.commit()``。
        """
        return self._session_factory()

    def dispose(self) -> None:
        """释放 Engine 维护的全部数据库连接，用于应用关闭阶段。"""
        self.engine.dispose()
