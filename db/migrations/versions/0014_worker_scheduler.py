"""Autonomy layer: outbox dispatcher claims, schedule firings, demo clock.

Revision ID: 0014_worker_scheduler
Revises: 0012_a09_corrections
Create Date: 2026-10-24

Mirrors services/api/app/models_worker.py. Three tables, no change to any
existing one -- in particular nothing here widens or narrows what the
restricted `encore_app` role may do to `outbox_intents`, `business_events`
or any other table an earlier migration protected.

All three tables are deliberately MUTABLE for encore_app (they get the
default privileges migration 0003 grants, so this migration adds no GRANT):

  - `outbox_dispatch_state` is a worker claim/lease/retry row. The claim is
    `UPDATE ... WHERE intent_id = (SELECT ... FOR UPDATE OF s SKIP LOCKED)`,
    which needs UPDATE on THIS table only -- that is exactly why the
    dispatcher's lease does not live on `outbox_intents`.
  - `schedule_firings` carries a lease too, so an occurrence whose worker
    died is retried instead of being lost; the exactly-once key is
    UNIQUE(job_name, scheduled_for), and the INSERT is the mutual exclusion.
  - `demo_clock` is a single pointer row, like `stream_heads`.

The audit record of what the dispatcher and the scheduler did stays in
`business_events`, which is append-only for encore_app and is untouched
here. Numbering: 0013 is reserved for terms resolution landing separately,
so this is 0014 on top of 0012.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_worker_scheduler"
down_revision: str | None = "0013_terms_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_dispatch_state",
        sa.Column("intent_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_disposition", sa.String(length=20), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["intent_id"], ["outbox_intents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("intent_id"),
    )
    # The claim's own ORDER BY/WHERE path: claimable rows only.
    op.create_index(
        "ix_outbox_dispatch_state_claimable",
        "outbox_dispatch_state",
        ["status", "next_attempt_at", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "schedule_firings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("job_name", sa.String(length=60), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("clock_mode", sa.String(length=20), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_name", "scheduled_for", name="uq_schedule_firing_occurrence"),
    )
    op.create_index("ix_schedule_firings_job_name", "schedule_firings", ["job_name"], unique=False)
    op.create_index("ix_schedule_firings_run_id", "schedule_firings", ["run_id"], unique=False)
    op.create_table(
        "demo_clock",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("offset_seconds", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("demo_clock")
    op.drop_index("ix_schedule_firings_run_id", table_name="schedule_firings")
    op.drop_index("ix_schedule_firings_job_name", table_name="schedule_firings")
    op.drop_table("schedule_firings")
    op.drop_index("ix_outbox_dispatch_state_claimable", table_name="outbox_dispatch_state")
    op.drop_table("outbox_dispatch_state")
