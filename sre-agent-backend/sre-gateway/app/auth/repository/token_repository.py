"""Gateway Token 的 SQLAlchemy Repository。"""

from sqlalchemy import select, update

from app.auth.model import GatewayToken
from app.core.database import Database


def initialize_auth_tables(database: Database) -> None:
    """幂等创建 Repository 所管理的 Auth 数据表。"""
    database.create_tables(GatewayToken.__table__)


class TokenRepository:
    """封装 GatewayToken 的所有数据库读写操作。"""

    def __init__(self, database: Database) -> None:
        """保存数据库依赖，供每次操作创建短生命周期 Session。"""
        self.database = database

    def create(self, token_hash: str, created_at: str) -> GatewayToken:
        """写入一个仅包含 Hash 的新 Token 记录。"""
        record = GatewayToken(token_hash=token_hash, created_at=created_at)
        with self.database.session() as session:
            session.add(record)
            session.commit()
            return record

    def find_enabled_by_hash(self, token_hash: str) -> GatewayToken | None:
        """按照 Hash 查找一条尚未禁用的 Token 记录。"""
        with self.database.session() as session:
            return session.scalar(
                select(GatewayToken).where(
                    GatewayToken.token_hash == token_hash,
                    GatewayToken.disabled_at.is_(None),
                )
            )

    def disable_by_hash(self, token_hash: str, disabled_at: str) -> bool:
        """禁用指定 Hash 对应的有效 Token，返回是否实际更新一条记录。"""
        with self.database.session() as session:
            result = session.execute(
                update(GatewayToken)
                .where(
                    GatewayToken.token_hash == token_hash,
                    GatewayToken.disabled_at.is_(None),
                )
                .values(disabled_at=disabled_at)
            )
            session.commit()
        return result.rowcount == 1

