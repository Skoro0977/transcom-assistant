from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .settings import IS_SQLITE, settings


def uid():
    return uuid4().hex


def utcnow():
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, connect_args={'check_same_thread': False}
                       if IS_SQLITE else {}, pool_pre_ping=True)


@event.listens_for(engine, 'connect')
def configure_connection(connection, _):
    if IS_SQLITE:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=5000')


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def session_db():
    with SessionLocal() as session:
        yield session


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default='user')
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginAttempt(Base):
    __tablename__ = 'login_attempts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    window: Mapped[int] = mapped_column()


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)


class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='')
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Membership(Base):
    __tablename__ = 'memberships'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=True)
    can_review: Mapped[bool] = mapped_column(Boolean, default=True)
