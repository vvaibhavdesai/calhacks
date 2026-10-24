"""SQLAlchemy engine/session wiring against PostgreSQL only.

No SQLite fallback anywhere, per product constraint. If DATABASE_URL points at
anything other than a postgresql(+psycopg) driver, engine creation still
happens (SQLAlchemy would just fail to connect), but local/dev defaults and
docker-compose always provision real Postgres.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    return create_engine(url, pool_pre_ping=True, future=True)


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def reset_engine_cache_for_tests() -> None:
    """Test-only helper: force get_engine()/get_session_factory() to rebuild,
    needed when a test monkeypatches DATABASE_URL after this module has
    already been imported elsewhere in the process."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
