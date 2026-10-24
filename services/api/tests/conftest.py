"""Test fixtures.

Requires a real reachable PostgreSQL instance -- no SQLite fallback, per
product constraint. Set TEST_DATABASE_URL to point at a dedicated test
database (never the dev database: tests truncate all tables between cases).
In CI this is the ephemeral postgres service container; locally it is the
`encore_test` database created alongside the dev `encore` database (see
infra/docker-compose.yml and docs/adr/0001-runtime.md).
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_JWT_SECRET", "test-only-secret-not-for-production-use")

# A07 incident, fixed here: TEST_DATABASE_URL must unconditionally override
# an already-exported DATABASE_URL, never merely fill in a missing one.
# `os.environ.setdefault("DATABASE_URL", ...)` (the previous code here) only
# sets a value that is NOT already present -- so a developer who sources
# .env (which sets DATABASE_URL to the local dev database) before running
# `TEST_DATABASE_URL=... pytest` silently keeps DATABASE_URL pointed at the
# DEV database, because it was already set. The `engine` fixture below
# calls `Base.metadata.drop_all()` at session end -- against the dev
# database that is destructive, and is exactly what happened while
# verifying A07 (see docs/progress/A07-handoff.md). CI is unaffected: CI
# sets DATABASE_URL directly (no separate dev database exists there), so
# the `else` branch below still applies in that case.
_test_database_url = os.environ.get("TEST_DATABASE_URL")
if _test_database_url:
    os.environ["DATABASE_URL"] = _test_database_url
else:
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore_test",
    )
os.environ.setdefault("DOCUMENT_STORAGE_ROOT", "/tmp/encore-test-documents")
os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402
from app.db import Base, get_db, make_engine  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Second, independent backstop (belt-and-suspenders after the fix above):
# refuse outright to run create_all/drop_all against a database whose name
# doesn't look like a dedicated test database. This is a hard-coded
# denylist of this repository's actual local dev database name, not a
# generic heuristic -- the goal is specifically "never again silently wipe
# ./encore locally", not a general-purpose safety framework.
_KNOWN_DEV_DATABASE_NAMES = ("encore",)


@pytest.fixture(scope="session")
def engine():
    eng = make_engine()
    db_name = eng.url.database or ""
    if db_name in _KNOWN_DEV_DATABASE_NAMES or "test" not in db_name:
        raise RuntimeError(
            f"refusing to run the test suite against database {db_name!r} -- it does "
            "not look like a dedicated test database (expected 'test' in the name, and "
            f"it must not be {_KNOWN_DEV_DATABASE_NAMES!r}). This fixture calls "
            "Base.metadata.drop_all() at session end, which would destroy that "
            "database's schema. Set TEST_DATABASE_URL to a real test database."
        )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db_session(engine) -> Session:
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        # Truncate everything so tests don't leak state across cases.
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        session.close()


@pytest.fixture()
def client(engine, db_session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
