"""Create a restricted `encore_app` Postgres role and REVOKE UPDATE/DELETE
on the append-only audit/journal tables for it.

Revision ID: 0003_app_role_privileges
Revises: 0002_canonical_records
Create Date: 2026-10-24

Why this exists (A02 acceptance criterion): "the application's normal DB
role must not be able to UPDATE or DELETE rows in the committed journal/
audit-event tables ... enforce this for real (e.g. a Postgres REVOKE/
trigger/rule), not just 'the ORM doesn't expose an update method'."

This migration runs as the schema-owning role (the same role
`DATABASE_URL` already uses for migrations -- see db/migrations/env.py),
which is the only role with DDL/GRANT rights. It creates a second, more
restricted login role, `encore_app`, intended for the actually-running
API/worker processes (see infra/docker-compose.yml: the `api`/`worker`
services connect as `encore_app`; the `migrate` service keeps using the
owning role). `encore_app` gets ordinary SELECT/INSERT/UPDATE/DELETE on most
tables (it is NOT a read-only role -- it still needs to write orders,
payouts, case status transitions, etc.) but UPDATE and DELETE are
explicitly revoked on:
  - business_events   (the audit event log itself)
  - source_versions    (immutable raw source storage)
  - journal_entries     (posted accounting journal)
  - journal_lines       (posted accounting journal lines)

This is a real, enforced Postgres privilege, verified against the live
database (not just described) in
services/api/tests/test_db_privileges.py, which opens an actual connection
AS `encore_app` and asserts UPDATE/DELETE on these tables raises a
permission-denied error from Postgres itself.

Trust boundary, honestly stated (per C06 / AGENT_HANDOFF.md): this REVOKE
stops the *application* role from mutating committed history. It does NOT
stop a Postgres superuser/database administrator (who could `ALTER TABLE`,
re-GRANT, or edit rows directly) and does NOT stop someone with access to
restore a tampered backup over the real database. See
docs/notes/audit-trust-boundary.md for the full statement of what this
control does and does not guarantee.

Password handling: `encore_app`'s password comes from the
`ENCORE_APP_DB_PASSWORD` environment variable, falling back to a documented
local-dev default (same pattern already used for `POSTGRES_PASSWORD` in
.env.example -- a committed *dev* default, not a production secret; a real
deployment overrides it, per A10).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_app_role_privileges"
down_revision: str | None = "0002_canonical_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_DEFAULT_APP_DB_PASSWORD = "encore_app_dev_password"

_PROTECTED_TABLES = ("business_events", "source_versions", "journal_entries", "journal_lines")


def _app_password() -> str:
    # Single quotes doubled for safe embedding in a SQL string literal.
    # This value comes from a trusted operator-controlled environment
    # variable (same trust level as POSTGRES_PASSWORD already committed as
    # a dev default in .env.example), never from end-user input.
    return os.environ.get("ENCORE_APP_DB_PASSWORD", _DEFAULT_APP_DB_PASSWORD).replace("'", "''")


def upgrade() -> None:
    conn = op.get_bind()
    password = _app_password()
    database_name = conn.engine.url.database
    owner_role = conn.engine.url.username

    role_exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
    ).scalar()
    if role_exists:
        conn.execute(sa.text(f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{password}'"))
    else:
        conn.execute(sa.text(f"CREATE ROLE {APP_ROLE} WITH LOGIN PASSWORD '{password}'"))

    conn.execute(sa.text(f'GRANT CONNECT ON DATABASE "{database_name}" TO {APP_ROLE}'))
    conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))

    # Broad default: encore_app can read/write ordinary domain tables.
    conn.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE " f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )
    )
    # Future tables created by the owning/migration role also get this
    # default automatically, so later migrations don't need to repeat the
    # GRANT (they DO need to repeat the protective REVOKE below if they add
    # another append-only table -- see docs/notes/audit-trust-boundary.md).
    conn.execute(
        sa.text(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner_role}" IN SCHEMA public '
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        )
    )

    # The actual enforced restriction: no UPDATE/DELETE on append-only
    # audit/journal tables for the application role.
    protected = ", ".join(_PROTECTED_TABLES)
    conn.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    conn = op.get_bind()
    protected = ", ".join(_PROTECTED_TABLES)
    conn.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))
    conn.execute(sa.text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}"))
    conn.execute(sa.text(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}"))
    database_name = conn.engine.url.database
    conn.execute(sa.text(f'REVOKE CONNECT ON DATABASE "{database_name}" FROM {APP_ROLE}'))
    # Role itself is intentionally left in place on downgrade (dropping a
    # role that may own a live connection mid-test is unnecessarily
    # destructive); re-running upgrade() is idempotent either way.
