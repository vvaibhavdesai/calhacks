"""A08 gap closure: cash-office counts, deposit slips/bags, chargeback fees.

Revision ID: 0010_a08_cash_deposits
Revises: 0009_agent_extractions
Create Date: 2026-10-24

Mirrors services/api/app/models_cash_office.py. New tables only; 0001-0009
are never edited and no existing table is altered.

All four tables are append-only for encore_app (REVOKE UPDATE/DELETE): a
count, slip, bag or fee notice is inserted once, complete.

MERGE-ORDER RISK: this revision is written on top of `0009_agent_extractions`
(the head of `integration` at 950eb34). Other branches cut from the same
head may also add a revision whose down_revision is 0009. Whichever merges
second must re-point its `down_revision` to the other's revision id (or add
an `alembic merge` revision); otherwise `alembic upgrade head` fails with
"multiple heads". Nothing in this file depends on another branch's tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_a08_cash_deposits"
down_revision: str | None = "0009_agent_extractions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = (
    "cash_office_counts",
    "cash_deposit_slips",
    "cash_deposit_bags",
    "chargeback_fees",
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "cash_office_counts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", sa.String(length=32), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("bag_reference", sa.String(length=200), nullable=False),
        sa.Column("counted_cents", sa.BigInteger(), nullable=False),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counted_by", sa.String(length=200), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("festival_id", "bag_reference", name="uq_cash_office_count_bag"),
    )
    for col in ("festival_id", "vendor_id", "business_date"):
        op.create_index(
            op.f(f"ix_cash_office_counts_{col}"), "cash_office_counts", [col], unique=False
        )

    op.create_table(
        "cash_deposit_slips",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("slip_reference", sa.String(length=200), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("stated_total_cents", sa.BigInteger(), nullable=False),
        sa.Column("bank_entry_id", sa.String(length=32), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["bank_entry_id"], ["bank_entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("festival_id", "slip_reference", name="uq_cash_deposit_slip_ref"),
    )
    for col in ("festival_id", "business_date"):
        op.create_index(
            op.f(f"ix_cash_deposit_slips_{col}"), "cash_deposit_slips", [col], unique=False
        )

    op.create_table(
        "cash_deposit_bags",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("slip_id", sa.String(length=32), nullable=False),
        sa.Column("bag_reference", sa.String(length=200), nullable=False),
        sa.Column("cash_count_id", sa.String(length=32), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["slip_id"], ["cash_deposit_slips.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cash_count_id"], ["cash_office_counts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slip_id", "bag_reference", name="uq_cash_deposit_bag"),
    )
    op.create_index(
        op.f("ix_cash_deposit_bags_slip_id"), "cash_deposit_bags", ["slip_id"], unique=False
    )

    op.create_table(
        "chargeback_fees",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=32), nullable=False),
        sa.Column("vendor_id", sa.String(length=32), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("bank_entry_id", sa.String(length=32), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bank_entry_id"], ["bank_entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_id", name="uq_chargeback_fee_payment"),
    )
    for col in ("festival_id", "vendor_id", "business_date"):
        op.create_index(op.f(f"ix_chargeback_fees_{col}"), "chargeback_fees", [col], unique=False)

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))
    for col in ("festival_id", "vendor_id", "business_date"):
        op.drop_index(op.f(f"ix_chargeback_fees_{col}"), table_name="chargeback_fees")
    op.drop_table("chargeback_fees")
    op.drop_index(op.f("ix_cash_deposit_bags_slip_id"), table_name="cash_deposit_bags")
    op.drop_table("cash_deposit_bags")
    for col in ("festival_id", "business_date"):
        op.drop_index(op.f(f"ix_cash_deposit_slips_{col}"), table_name="cash_deposit_slips")
    op.drop_table("cash_deposit_slips")
    for col in ("festival_id", "vendor_id", "business_date"):
        op.drop_index(op.f(f"ix_cash_office_counts_{col}"), table_name="cash_office_counts")
    op.drop_table("cash_office_counts")
