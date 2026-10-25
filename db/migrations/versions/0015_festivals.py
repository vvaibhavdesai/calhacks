"""The festival record: one table, nothing else touched.

Revision ID: 0015_festivals
Revises: 0014_worker_scheduler
Create Date: 2026-10-24

Mirrors services/api/app/models_festival.py. Strictly additive: no existing
table is altered, no existing column is widened or re-typed, and no
foreign key is added from `festivals` to -- or from any existing table to
`festivals`. That last point is deliberate rather than an omission:
`festival_id` already carries values on ~25 tables that were inserted
before a festival registry existed (the dev fixtures' "gcmf-2026-a03dev",
"gcmf-2026-a05dev", "gcmf-2026-a07dev-main", ...), so a foreign key would
either fail on existing data or force a fabricated backfill row per id.
Instead `festivals.id` IS that externally minted string, and
app/festivals.py serves a derived record for any id that has data but no
row here.

No seed row. The festival in AGENT_HANDOFF.md (Golden Coast Music Festival,
October 16-18 2026) is real documentation, but its id is not: every
existing row uses a fixture-specific id, so inserting one canonical id here
would create a festival that resolves to zero records while the ids that DO
have records stayed unnamed. Registration is an explicit act
(`PUT /api/v1/festivals/{festival_id}`), audited like any other.

Privileges: `festivals` is configuration, not committed history, so
encore_app may INSERT/UPDATE it and this migration adds no GRANT (migration
0003's ALTER DEFAULT PRIVILEGES already covers tables created later) and no
REVOKE. The audit trail of a registration is a `business_events` row, and
that table's append-only REVOKE from migration 0003 is untouched. Nothing
here takes a row lock, so the `SELECT ... FOR UPDATE`-needs-UPDATE caveat
does not arise: the registration upsert is keyed on the primary key, and
the one lock in its transaction is the existing `stream_heads` lock
app/uow.py already takes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_festivals"
down_revision: str | None = "0014_worker_scheduler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "festivals",
        # Not a generated id: the externally minted festival_id string that
        # every festival-scoped table already carries, String(64) to match
        # those columns exactly.
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("organizer_name", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("fictional_notice", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("festivals")
