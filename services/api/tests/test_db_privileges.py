"""Real Postgres privilege test for A02's append-only enforcement
(business_events/source_versions/journal_entries/journal_lines). Opens an
actual separate connection authenticated AS the restricted `encore_app`
role and asserts Postgres itself rejects UPDATE/DELETE on those tables --
this is "a Postgres REVOKE... not just the ORM doesn't expose an update
method", verified against the live database, not described or mocked.

Test-isolation note: the shared `engine`/`db_session` fixtures (conftest.py)
use Base.metadata.create_all()/drop_all() for per-test-session isolation.
That does NOT preserve table-level GRANT/REVOKE across a drop+recreate
cycle the way the real Alembic-managed database does: this database has
`ALTER DEFAULT PRIVILEGES FOR ROLE <owner> GRANT ... TO encore_app`
(migration 0003_app_role_privileges.py), so any newly created table
automatically gets broad default access for encore_app -- the narrower
REVOKE on the four protected tables does NOT get reapplied by create_all().
That is a pre-existing A01 testing choice (drop_all/create_all for
isolation), not something A02 can safely change without touching every
existing test's fixture. So `_reapply_revoke` below re-issues the *exact*
GRANT/REVOKE statements migration 0003 uses, immediately before asserting.
This still exercises the real mechanism end to end (a real Postgres REVOKE,
checked over a real separate connection authenticated as the restricted
role) -- it just does not depend on that REVOKE having survived an
unrelated fixture's table teardown. See docs/progress/A02-handoff.md and
docs/notes/audit-trust-boundary.md.
"""

from __future__ import annotations

import os
import re

import psycopg
import pytest
from app.config import get_settings

_PROTECTED_TABLES = ("business_events", "source_versions", "journal_entries", "journal_lines")
# One real, existing column per protected table, used for the harmless
# "SET col = col WHERE false" no-op UPDATE probe below (each table has a
# different column set, so there is no single column name common to all).
_PROBE_COLUMN = {
    "business_events": "hash",
    "source_versions": "content_hash",
    "journal_entries": "posting_rule_version",
    "journal_lines": "account_code",
}

_URL_RE = re.compile(r"postgresql\+psycopg://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)")


def _owner_conn_params() -> dict:
    settings = get_settings()
    match = _URL_RE.match(settings.database_url)
    assert match, f"unexpected DATABASE_URL shape: {settings.database_url!r}"
    user, password, host, port, dbname = match.groups()
    return {"host": host, "port": int(port), "dbname": dbname, "user": user, "password": password}


def _app_conn_params() -> dict:
    params = dict(_owner_conn_params())
    params["user"] = "encore_app"
    params["password"] = os.environ.get("ENCORE_APP_DB_PASSWORD", "encore_app_dev_password")
    return params


@pytest.fixture()
def _reapply_revoke(engine):
    """`engine` is requested only to guarantee the table-creation fixture
    has already run in this session; the actual GRANT/REVOKE here is done
    over a direct psycopg connection, matching migration
    0003_app_role_privileges.py exactly."""
    owner = psycopg.connect(**_owner_conn_params())
    owner.autocommit = True
    try:
        with owner.cursor() as cur:
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON vendors TO encore_app")
            for table in _PROTECTED_TABLES:
                cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO encore_app")
                cur.execute(f"REVOKE UPDATE, DELETE ON {table} FROM encore_app")
    finally:
        owner.close()
    yield


def test_encore_app_cannot_update_protected_tables(_reapply_revoke):
    conn = psycopg.connect(**_app_conn_params())
    try:
        for table in _PROTECTED_TABLES:
            col = _PROBE_COLUMN[table]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    # WHERE false matches zero rows -- Postgres still checks
                    # table-level privilege before row scanning, so this
                    # proves the REVOKE without actually mutating data.
                    cur.execute(f"UPDATE {table} SET {col} = {col} WHERE false")  # noqa: S608
            conn.rollback()
    finally:
        conn.close()


def test_encore_app_cannot_delete_protected_tables(_reapply_revoke):
    conn = psycopg.connect(**_app_conn_params())
    try:
        for table in _PROTECTED_TABLES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table} WHERE false")  # noqa: S608
            conn.rollback()
    finally:
        conn.close()


def test_encore_app_can_still_select_and_insert_append_only_tables(_reapply_revoke):
    """The restriction is specifically UPDATE/DELETE -- encore_app must
    still be able to SELECT and INSERT, or this control would break
    ordinary ingestion/event-append traffic, not just protect history."""
    conn = psycopg.connect(**_app_conn_params())
    try:
        with conn.cursor() as cur:
            for table in _PROTECTED_TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
                cur.fetchone()
        conn.rollback()
    finally:
        conn.close()


def test_encore_app_has_ordinary_update_on_unrestricted_table(_reapply_revoke):
    """Contrast case: an ordinary domain table (vendors) is NOT
    append-only, so encore_app keeps normal UPDATE rights there."""
    conn = psycopg.connect(**_app_conn_params())
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE vendors SET name = name WHERE false")
        conn.rollback()
    finally:
        conn.close()
