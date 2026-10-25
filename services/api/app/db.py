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
    # Every connection speaks UTC, whatever the server's own timezone is.
    #
    # CONTRACTS.md C01: an instant on the wire is UTC. This database runs in
    # America/Los_Angeles, so `timestamptz` columns came back Pacific-aware
    # and serialised as "...-07:00" -- which the web client rejects as
    # "Invalid time" by design (apps/web/src/lib/time.ts: "a non-zero offset
    # is not: an instant on the wire is UTC"). /events agreed with the
    # contract because it builds its timestamps in Python; /cases and
    # /journal did not, because theirs come straight from the ORM.
    #
    # Safe to set globally: nothing in this codebase derives a calendar date
    # from a timestamp in SQL (no `::date`, `date_trunc` or `CURRENT_DATE`
    # anywhere), so no business date can shift. Business dates are `date`
    # columns, and the festival-local reasoning that produces them lives in
    # Python with explicit zones (app/demo_clock.py, app/terms_resolution.py).
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": "-c timezone=utc"},
    )


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
