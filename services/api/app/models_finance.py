"""A08 payables, accounting, tax, cash and reports: new tables.

Scope note, same convention as app/models_settlement.py and
app/models_payout_dispatch.py: additive columns on EXISTING tables
(`journal_entries`, `journal_lines`, `suppliers`, `supplier_invoices`,
`order_lines`) are added directly to their classes in app/models_ledger.py
(migration 0008). This module holds only the genuinely new tables.

Posting map: `Account` rows are a versioned chart (`chart_version`), seeded
lazily by app/accounting.py's `ensure_chart`. Codes and roles deliberately
match the DRAFT posting map lane B's independent oracle uses
(tests/oracle/oracle_full.py on `lane-b`), so A-track journals can later be
scored against it by ROLE. That shared map is a C02 convention, still DRAFT
and not user-approved; the oracle's arithmetic is NOT reused here.

Append-only tables (REVOKE UPDATE/DELETE from encore_app in migration
0008): `supplier_payments`, `vendor_statements`, `daily_closes`. A decision,
payment or issued statement is never edited; a later version is a new row.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id

# ---------------------------------------------------------------------------
# Chart of accounts / posting rule versions
# ---------------------------------------------------------------------------


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("chart_version", "code", name="uq_account_chart_code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    chart_version: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # ASSET | LIABILITY | EQUITY | REVENUE | EXPENSE
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PostingRuleVersion(Base):
    __tablename__ = "posting_rule_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    version: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    chart_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Finance configuration (tax, schedule, forecast horizon), versioned
# ---------------------------------------------------------------------------


class FinanceConfig(Base):
    """Versioned per festival; every tax summary / statement / close pins
    the exact version it used. A new configuration is a new row, never an
    edit (`status` moves ACTIVE -> SUPERSEDED on the old row only)."""

    __tablename__ = "finance_configs"
    __table_args__ = (
        UniqueConstraint("festival_id", "version", name="uq_finance_config_festival_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    statement_local_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "09:00"
    payout_local_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "10:00"
    forecast_horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # {"label": ..., "rates_by_category": {"prepared_food": "0.0925", ...},
    #  "exempt_categories": [...], "liability_owner_by_custody": {...}}
    tax: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Payables
# ---------------------------------------------------------------------------


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("supplier_id", "po_number", name="uq_po_supplier_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    po_number: Mapped[str] = mapped_column(String(100), nullable=False)
    po_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Receipt/service evidence where relevant (delivery note, sign-off).
    receipt_evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SupplierPayment(Base):
    """Approved simulated supplier payment. Append-only. `idempotency_key`
    is unique, so a retried request can never create a second payment;
    app/payables.py additionally row-locks the invoice and re-checks the
    outstanding balance, so two different requests can never jointly pay
    more than is owed."""

    __tablename__ = "supplier_payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("supplier_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PAID_SIMULATED")
    approved_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_capability: Mapped[str] = mapped_column(String(100), nullable=False)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Other organizer income / expenses / opening balances
# ---------------------------------------------------------------------------


class OpeningBalance(Base):
    __tablename__ = "opening_balances"
    __table_args__ = (
        UniqueConstraint("festival_id", "account_role", name="uq_opening_balance_role"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_role: Mapped[str] = mapped_column(String(60), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TicketSettlement(Base):
    __tablename__ = "ticket_settlements"
    __table_args__ = (
        UniqueConstraint("festival_id", "settlement_reference", name="uq_ticket_settlement_ref"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(100), nullable=False)
    settlement_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    platform_fees_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    bank_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SponsorInvoice(Base):
    __tablename__ = "sponsor_invoices"
    __table_args__ = (
        UniqueConstraint("festival_id", "invoice_number", name="uq_sponsor_invoice_number"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sponsor_name: Mapped[str] = mapped_column(String(300), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SponsorshipReceipt(Base):
    __tablename__ = "sponsorship_receipts"
    __table_args__ = (
        UniqueConstraint("festival_id", "receipt_reference", name="uq_sponsorship_receipt_ref"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sponsor_invoice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sponsor_invoices.id", ondelete="RESTRICT"), nullable=False
    )
    receipt_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    bank_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("festival_id", "expense_reference", name="uq_expense_reference"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expense_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    bank_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Reports: vendor statements and daily close (append-only)
# ---------------------------------------------------------------------------


class VendorStatement(Base):
    """One row per issued statement version. PROVISIONAL/HELD statements
    are issued by the configured statement time (default 09:00) even before
    the 10:00 payout; a FINAL version is issued after payout confirmation.
    A later version links `supersedes_statement_id`; nothing is edited."""

    __tablename__ = "vendor_statements"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id", "business_date", "statement_version", name="uq_vendor_statement_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    statement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # PROVISIONAL|HELD|FINAL
    settlement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payout_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    finance_config_id: Mapped[str] = mapped_column(String(32), nullable=False)
    due_by: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_on_time: Mapped[bool] = mapped_column(nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    supersedes_statement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issued_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DailyClose(Base):
    __tablename__ = "daily_closes"
    __table_args__ = (
        UniqueConstraint("festival_id", "business_date", name="uq_daily_close_festival_date"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    trial_balance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checks: Mapped[dict] = mapped_column(JSONB, nullable=False)
    journal_entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    closed_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
