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
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore_test",
    ),
)
os.environ.setdefault("DOCUMENT_STORAGE_ROOT", "/tmp/encore-test-documents")
os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402
from app.db import Base, get_db, make_engine  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    eng = make_engine()
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
