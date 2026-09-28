"""Database access isolated behind a repository so service tests do not require MySQL."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.model.user import User


engine = create_engine(settings.database_url, pool_size=4, max_overflow=2, pool_pre_ping=True, pool_timeout=0.5, connect_args={"connect_timeout":1,"read_timeout":1,"write_timeout":1})
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


class UserRepository:
    """Perform bounded primary-key and indexed email reads using short sessions."""

    def find_by_id(self, user_id: int) -> User | None:
        with SessionFactory() as session:
            return session.get(User, user_id)

    def find_by_email(self, email: str) -> User | None:
        with SessionFactory() as session:
            return session.scalar(select(User).where(User.email == email).limit(1))

    def list_after(self, after_id: int, limit: int) -> list[User]:
        with SessionFactory() as session:
            statement = select(User).where(User.id > after_id).order_by(User.id).limit(limit)
            return list(session.scalars(statement))
