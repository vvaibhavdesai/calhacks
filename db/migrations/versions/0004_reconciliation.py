"""A03 reconciliation: bank_entries, cash_counts, match_groups,
match_group_members, plus two additive columns on `payments`
(`kind`, `bank_entry_id`).

Revision ID: 0004_reconciliation
Revises: 0003_app_role_privileges
Create Date: 2026-10-24

Mirrors services/api/app/models_reconciliation.py and the two new columns
added to Payment in services/api/app/models_ledger.py. Built on top of
0003_app_role_privileges per git discipline -- 0001/0002/0003 are never
edited here, only added to.

`encore_app` (the restricted application role created in 0003) already has
default SELECT/INSERT/UPDATE/DELETE on any new table created by the owning
role, via the `ALTER DEFAULT PRIVILEGES` set up there -- no new GRANT
statements are needed here. None of A03's new tables are append-only in the
audit sense (match_groups/cases can legitimately be re-evaluated by a later
reconciliation run), so none of them join `business_events`/`source_versions`
/`journal_entries`/`journal_lines` in the protected-table REVOKE list.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_reconciliation"
down_revision: str | None = "0003_app_role_privileges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- bank_entries -------------------------------------------------
    op.create_table(
        "bank_entries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("provider_reference", sa.String(length=300), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False, server_default="credit"),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("batch_gross_cents", sa.BigInteger(), nullable=True),
        sa.Column("batch_fee_cents", sa.BigInteger(), nullable=True),
        sa.Column(
            "settlement_status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_document_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("locator_kind", sa.String(length=20), nullable=True),
        sa.Column("locator", sa.String(length=300), nullable=True),
        sa.Column("quoted_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_bank_entries_festival_id", "bank_entries", ["festival_id"])
    op.create_index("ix_bank_entries_business_date", "bank_entries", ["business_date"])

    # --- cash_counts ----------------------------------------------------
    op.create_table(
        "cash_counts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("counted_cents", sa.BigInteger(), nullable=False),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("counted_by", sa.String(length=200), nullable=True),
        sa.Column(
            "source_document_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("locator_kind", sa.String(length=20), nullable=True),
        sa.Column("locator", sa.String(length=300), nullable=True),
        sa.Column("quoted_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_cash_counts_vendor_id", "cash_counts", ["vendor_id"])
    op.create_index("ix_cash_counts_business_date", "cash_counts", ["business_date"])

    # --- payments: additive columns -------------------------------------
    op.add_column(
        "payments",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="capture"),
    )
    op.add_column(
        "payments",
        sa.Column(
            "bank_entry_id",
            sa.String(length=32),
            sa.ForeignKey("bank_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_payments_bank_entry_id", "payments", ["bank_entry_id"])

    # --- match_groups / match_group_members ------------------------------
    op.create_table(
        "match_groups",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=32),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("match_type", sa.String(length=30), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=True),
        sa.Column("matched_amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("variance_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="matched"),
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
    op.create_index("ix_match_groups_run_id", "match_groups", ["run_id"])
    op.create_index("ix_match_groups_festival_id", "match_groups", ["festival_id"])
    op.create_index("ix_match_groups_vendor_id", "match_groups", ["vendor_id"])
    op.create_index("ix_match_groups_match_type", "match_groups", ["match_type"])
    op.create_index("ix_match_groups_business_date", "match_groups", ["business_date"])
    op.create_index("ix_match_groups_case_id", "match_groups", ["case_id"])

    op.create_table(
        "match_group_members",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "match_group_id",
            sa.String(length=32),
            sa.ForeignKey("match_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("member_kind", sa.String(length=30), nullable=False),
        sa.Column("member_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_match_group_members_match_group_id", "match_group_members", ["match_group_id"]
    )
    op.create_index("ix_match_group_members_member_id", "match_group_members", ["member_id"])


def downgrade() -> None:
    op.drop_table("match_group_members")
    op.drop_table("match_groups")
    op.drop_index("ix_payments_bank_entry_id", table_name="payments")
    op.drop_column("payments", "bank_entry_id")
    op.drop_column("payments", "kind")
    op.drop_table("cash_counts")
    op.drop_table("bank_entries")
