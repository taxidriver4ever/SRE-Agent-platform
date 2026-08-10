"""Auth 模块的数据访问层。

Repository 是 Auth 模块中唯一允许直接编写 SQLAlchemy 查询和提交事务的层。
"""

from .token_repository import TokenRepository, initialize_auth_tables

__all__ = ["TokenRepository", "initialize_auth_tables"]

