"""Payables: PO lines, delivery receipts, supplier deposits, supplier quotes.

Revision ID: 0017_payables_lines_receipts_deposits_quotes
Revises: 0016_audit_append_only
Create Date: 2026-10-25

Five new tables plus one additive column, all native A08 payables
concepts -- no parallel schema (no contract_obligation/obligation_link).
0001-0016 are never edited here.

1. `purchase_order_lines` -- a PO's line items (item/qty/unit price). POs
   without lines (every PO before this) are unaffected: `create_purchase_order`
   keeps working exactly as before when called with no `lines` argument.
2. `delivery_receipts` -- one row per accepted delivery against a PO line.
   Append-only (REVOKE UPDATE/DELETE): a receipt is recorded once, a
   mistaken one is never edited, only superseded by a new fact elsewhere.
3. `supplier_deposits` / `supplier_deposit_applications` -- a paid deposit
   and its (partial) application against one or more invoices. Both
   append-only: a deposit is drawn once (unique idempotency_key), an
   application is recorded once per (deposit, invoice) pair.
4. `supplier_quotes` -- versioned competing-supplier quotes (read-mostly,
   made append-only for the same reason as the above: a revised quote is
   a NEW version row with `supersedes_id`, never an edit of the old one).
5. `supplier_invoices.deposit_applied_cents` -- additive column. Outstanding
   becomes gross - credited - paid - deposit_applied (domain.payables.
   InvoiceBalance); existing rows default to 0, unaffected.

Account 1300 (SUPPLIER_DEPOSITS, ASSET) is added in domain.accounting's
Python ACCOUNTS dict, not as a migrated table -- `ensure_chart` upserts it
lazily, same pattern as every other account code in this codebase.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_payables_v2"  # alembic_version.version_num is varchar(32); the long
# descriptive name above is this migration's filename, matching the spec's
# literal "0017_payables_lines_receipts_deposits_quotes", but the `revision`
# identifier itself must stay short.
down_revision: str | None = "0016_audit_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = (
    "delivery_receipts",
    "supplier_deposits",
    "supplier_deposit_applications",
    "supplier_quotes",
)


def upgrade() -> None:
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "po_id",
            sa.String(length=32),
            sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("item_code", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("line_cents", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "line_cents = quantity * unit_price_cents", name="ck_po_line_cents_consistent"
        ),
        sa.UniqueConstraint("po_id", "line_no", name="uq_po_line_no"),
    )
    op.create_index("ix_purchase_order_lines_po_id", "purchase_order_lines", ["po_id"])

    op.create_table(
        "delivery_receipts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "po_line_id",
            sa.String(length=32),
            sa.ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_delivery_receipt_quantity_positive"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_delivery_receipts_po_line_id", "delivery_receipts", ["po_line_id"])

    op.create_table(
        "supplier_deposits",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column(
            "supplier_id",
            sa.String(length=32),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "po_id",
            sa.String(length=32),
            sa.ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_business_date", sa.Date(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("journal_entry_id", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_supplier_deposits_festival_id", "supplier_deposits", ["festival_id"])
    op.create_index("ix_supplier_deposits_supplier_id", "supplier_deposits", ["supplier_id"])

    op.create_table(
        "supplier_deposit_applications",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "deposit_id",
            sa.String(length=32),
            sa.ForeignKey("supplier_deposits.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            sa.String(length=32),
            sa.ForeignKey("supplier_invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("journal_entry_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("deposit_id", "invoice_id", name="uq_deposit_application"),
    )
    op.create_index(
        "ix_supplier_deposit_applications_deposit_id",
        "supplier_deposit_applications",
        ["deposit_id"],
    )
    op.create_index(
        "ix_supplier_deposit_applications_invoice_id",
        "supplier_deposit_applications",
        ["invoice_id"],
    )

    op.create_table(
        "supplier_quotes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "supplier_id",
            sa.String(length=32),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "supersedes_id",
            sa.String(length=32),
            sa.ForeignKey("supplier_quotes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_cents", sa.BigInteger(), nullable=True),
        sa.Column("delivery_cents", sa.BigInteger(), nullable=True),
        sa.Column("tax_cents", sa.BigInteger(), nullable=True),
        sa.Column("deposit_cents", sa.BigInteger(), nullable=True),
        sa.Column("deposit_due", sa.Date(), nullable=True),
        sa.Column("balance_due", sa.Date(), nullable=True),
        sa.Column("insurance_evidence", sa.Boolean(), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "supplier_id", "request_key", "version", name="uq_supplier_quote_version"
        ),
    )
    op.create_index("ix_supplier_quotes_supplier_id", "supplier_quotes", ["supplier_id"])
    op.create_index("ix_supplier_quotes_request_key", "supplier_quotes", ["request_key"])

    op.add_column(
        "supplier_invoices",
        sa.Column("deposit_applied_cents", sa.BigInteger(), nullable=False, server_default="0"),
    )

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_column("supplier_invoices", "deposit_applied_cents")

    op.drop_index("ix_supplier_quotes_request_key", table_name="supplier_quotes")
    op.drop_index("ix_supplier_quotes_supplier_id", table_name="supplier_quotes")
    op.drop_table("supplier_quotes")

    op.drop_index(
        "ix_supplier_deposit_applications_invoice_id", table_name="supplier_deposit_applications"
    )
    op.drop_index(
        "ix_supplier_deposit_applications_deposit_id", table_name="supplier_deposit_applications"
    )
    op.drop_table("supplier_deposit_applications")

    op.drop_index("ix_supplier_deposits_supplier_id", table_name="supplier_deposits")
    op.drop_index("ix_supplier_deposits_festival_id", table_name="supplier_deposits")
    op.drop_table("supplier_deposits")

    op.drop_index("ix_delivery_receipts_po_line_id", table_name="delivery_receipts")
    op.drop_table("delivery_receipts")

    op.drop_index("ix_purchase_order_lines_po_id", table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")
