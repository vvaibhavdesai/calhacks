"""A07 controlled payouts, outbox dispatch and rail simulator.

Revision ID: 0007_payout_dispatch
Revises: 0006_case_policy_resolution
Create Date: 2026-10-24

Mirrors services/api/app/models_payout_dispatch.py (new tables) and the
additive columns added to `Payout`/`Vendor` in
services/api/app/models_ledger.py. Built on top of 0006 per git discipline
-- 0001-0006 are never edited here, only added to.

New tables: `cash_pools`, `vendor_day_payout_aggregates`,
`rail_simulator_records`, `payout_approvals`.

`encore_app` already has default SELECT/INSERT/UPDATE/DELETE on any new
table via migration 0003's `ALTER DEFAULT PRIVILEGES`. This migration
explicitly REVOKEs UPDATE/DELETE on `payout_approvals` only -- it is an
append-only decision log, joining `business_events`/`settlements`/etc.
`cash_pools` and `vendor_day_payout_aggregates` are legitimately mutable
aggregate state (same category as `stream_heads`): release's atomic
reservation logic UPDATEs them under `SELECT ... FOR UPDATE`, so they stay
unrestricted. `rail_simulator_records` is also left unrestricted
(mutable): it models a separate external system whose outcome can
legitimately transition (DELAYED/UNKNOWN -> a final outcome) via an
explicit reconciliation call -- see app/rail_simulator.py's module
docstring.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_payout_dispatch"
down_revision: str | None = "0006_case_policy_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = ("payout_approvals",)


def upgrade() -> None:
    # --- vendors: additive column -------------------------------------
    op.add_column(
        "vendors",
        sa.Column(
            "beneficiary_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # --- payouts: additive columns --------------------------------------
    op.add_column(
        "payouts", sa.Column("bank_debit_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payouts", sa.Column("payout_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payouts", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payouts",
        sa.Column(
            "cancel_requested_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "payouts",
        sa.Column("recovery_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("payouts", sa.Column("recovery_reason", sa.Text(), nullable=True))
    op.add_column(
        "payouts", sa.Column("reservation_released_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payouts", sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0")
    )

    # --- cash_pools -------------------------------------------------------
    op.create_table(
        "cash_pools",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("available_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("festival_id", name="uq_cash_pool_festival"),
    )

    # --- vendor_day_payout_aggregates --------------------------------------
    op.create_table(
        "vendor_day_payout_aggregates",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("committed_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("vendor_id", "business_date", name="uq_vendor_day_payout_aggregate"),
    )
    op.create_index(
        "ix_vendor_day_payout_aggregates_vendor_id",
        "vendor_day_payout_aggregates",
        ["vendor_id"],
    )
    op.create_index(
        "ix_vendor_day_payout_aggregates_business_date",
        "vendor_day_payout_aggregates",
        ["business_date"],
    )

    # --- rail_simulator_records --------------------------------------------
    op.create_table(
        "rail_simulator_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("vendor_id", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("provider_reference", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_rail_simulator_record_key"),
    )
    op.create_index("ix_rail_simulator_records_vendor_id", "rail_simulator_records", ["vendor_id"])

    # --- payout_approvals (append-only) ------------------------------------
    op.create_table(
        "payout_approvals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "payout_id",
            sa.String(length=32),
            sa.ForeignKey("payouts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_payout_approvals_payout_id", "payout_approvals", ["payout_id"])

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_index("ix_payout_approvals_payout_id", table_name="payout_approvals")
    op.drop_table("payout_approvals")

    op.drop_index("ix_rail_simulator_records_vendor_id", table_name="rail_simulator_records")
    op.drop_table("rail_simulator_records")

    op.drop_index(
        "ix_vendor_day_payout_aggregates_business_date",
        table_name="vendor_day_payout_aggregates",
    )
    op.drop_index(
        "ix_vendor_day_payout_aggregates_vendor_id", table_name="vendor_day_payout_aggregates"
    )
    op.drop_table("vendor_day_payout_aggregates")

    op.drop_table("cash_pools")

    op.drop_column("payouts", "dispatch_attempts")
    op.drop_column("payouts", "reservation_released_at")
    op.drop_column("payouts", "recovery_reason")
    op.drop_column("payouts", "recovery_required")
    op.drop_column("payouts", "cancel_requested_by")
    op.drop_column("payouts", "cancel_requested_at")
    op.drop_column("payouts", "payout_confirmed_at")
    op.drop_column("payouts", "bank_debit_confirmed_at")

    op.drop_column("vendors", "beneficiary_details")
