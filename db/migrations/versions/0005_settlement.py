"""A04 settlement: settlements, settlement_line_items, plus one additive
column on `payouts` (`settlement_id`).

Revision ID: 0005_settlement
Revises: 0004_reconciliation
Create Date: 2026-10-24

Mirrors services/api/app/models_settlement.py. Built on top of
0004_reconciliation per git discipline -- 0001/0002/0003/0004 are never
edited here, only added to.

`encore_app` (the restricted application role created in 0003) already has
default SELECT/INSERT/UPDATE/DELETE on any new table created by the owning
role, via the `ALTER DEFAULT PRIVILEGES` set up there. This migration then
explicitly REVOKEs UPDATE/DELETE on `settlements` and
`settlement_line_items`, joining `business_events`/`source_versions`/
`journal_entries`/`journal_lines` in the append-only/immutable-after-write
set -- per the task's "freeze the calculation ... every settlement row
must pin the exact calculation-version" and "a commission change after a
prior payout ... must produce a linked adjustment record, not silently
overwrite the original settlement" requirements. A correction is always a
NEW row with `correction_of` set, never an UPDATE -- this migration makes
that the only option available to the application role, not merely a
convention the service layer happens to follow.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_settlement"
down_revision: str | None = "0004_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = ("settlements", "settlement_line_items")


def upgrade() -> None:
    # --- settlements ------------------------------------------------------
    op.create_table(
        "settlements",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=32),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("calculation_version", sa.String(length=50), nullable=False),
        sa.Column(
            "vendor_contract_term_id",
            sa.String(length=32),
            sa.ForeignKey("vendor_contract_terms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vendor_contract_term_version", sa.Integer(), nullable=True),
        sa.Column("commission_bps_used", sa.Integer(), nullable=False),
        sa.Column("minimum_fee_cents_used", sa.BigInteger(), nullable=False),
        sa.Column("processor_fee_deductible_used", sa.Boolean(), nullable=False),
        sa.Column("is_final_period", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("eligible_sales_cents", sa.BigInteger(), nullable=False),
        sa.Column("centrally_held_cents", sa.BigInteger(), nullable=False),
        sa.Column("vendor_retained_cash_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tips_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cumulative_eligible_sales_cents", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_commission_assessed_prior_cents", sa.BigInteger(), nullable=False),
        sa.Column("assessed_fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("collected_fee_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "vendor_fee_receivable_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("unpaid_balance_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "processor_fee_assessed_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "processor_fee_collected_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("payout_cents", sa.BigInteger(), nullable=False),
        sa.Column("minimum_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "holds", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column(
            "policy_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "correction_of",
            sa.String(length=32),
            sa.ForeignKey("settlements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("correction_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="FINAL"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_settlements_festival_id", "settlements", ["festival_id"])
    op.create_index("ix_settlements_vendor_id", "settlements", ["vendor_id"])
    op.create_index("ix_settlements_business_date", "settlements", ["business_date"])
    op.create_index("ix_settlements_run_id", "settlements", ["run_id"])
    op.create_index("ix_settlements_correction_of", "settlements", ["correction_of"])

    # --- settlement_line_items --------------------------------------------
    op.create_table(
        "settlement_line_items",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "settlement_id",
            sa.String(length=32),
            sa.ForeignKey("settlements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_type", sa.String(length=40), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_settlement_line_items_settlement_id", "settlement_line_items", ["settlement_id"]
    )

    # --- payouts: additive column ------------------------------------------
    op.add_column(
        "payouts",
        sa.Column(
            "settlement_id",
            sa.String(length=32),
            sa.ForeignKey("settlements.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_payouts_settlement_id", "payouts", ["settlement_id"])

    # --- protect settlements/settlement_line_items like business_events ---
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_index("ix_payouts_settlement_id", table_name="payouts")
    op.drop_column("payouts", "settlement_id")
    op.drop_table("settlement_line_items")
    op.drop_table("settlements")
