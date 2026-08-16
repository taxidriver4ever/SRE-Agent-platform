"""SQLAlchemy mapping for the shared synthetic users table."""

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative metadata root; schema creation remains owned by the Infra repository."""


class User(Base):
    """User persistence entity. API schemas prevent this ORM object leaking to clients."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    membership_level: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[object] = mapped_column(DateTime)
