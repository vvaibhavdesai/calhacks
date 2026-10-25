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
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
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
# X0.3 (prevention extension): contract obligations and links
# ---------------------------------------------------------------------------


class ContractObligation(Base):
    """One deposit/milestone/final payment commitment under a contract
    (TICKET_SPEC.md X0.3). `amount_cents` is set once, at creation, and
    NEVER mutated afterward by anything in this ticket -- the hard
    requirement "it must never total $40,000" (i.e. a $20,000 commitment
    must always read back as $20,000, no matter what gets linked to it) is
    enforced by this column simply never being written to a second time,
    not by a runtime guard. Only `status`/`version`/`updated_at` are ever
    updated in place (status transitions, see
    packages/domain/domain/contract_obligations.py's explicit transition
    table); nothing here is append-only the way `obligation_link` below is.

    `contract_id` and the inlined SourceRef (`source_document_id`/
    `source_version`/`content_hash`/`locator_kind`/`locator`/
    `quoted_excerpt`) are deliberately two separate things, per
    TICKET_SPEC.md listing them as two separate bullets ("contract_id, ...,
    source document + clause"): `contract_id` is the stable identity of the
    overall contract this obligation belongs to (a `source_documents.id`
    with `document_kind="CONTRACT"`, by convention -- not DB-enforced, same
    "enforced in application code, not a constraint" precedent as every
    other loosely-typed kind column in this schema); the inlined SourceRef
    is the precise citation of exactly where THIS obligation's amount/date/
    condition is stated, which is USUALLY the same document but need not be
    -- e.g. a later amendment document that changes a milestone amount would
    cite itself here while `contract_id` keeps pointing at the original
    contract's identity. `counterparty_id` is deliberately NOT a foreign
    key (it points at `vendors.id` when `counterparty_type="vendor"` or
    `suppliers.id` when `counterparty_type="supplier"` -- no single FK
    target can express that), same "loose, validated-in-code pair"
    convention as `Case.subject_type`/`subject_id` (X0.1) and
    `Approval.subject_type`/`subject_id` (X0.1).
    """

    __tablename__ = "contract_obligations"
    __table_args__ = (
        Index("ix_contract_obligations_counterparty", "counterparty_type", "counterparty_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contract_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # "vendor" | "supplier" -- packages/domain/domain/contract_obligations.
    # COUNTERPARTY_TYPES is the single source of truth for this vocabulary.
    counterparty_type: Mapped[str] = mapped_column(String(20), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # "deposit" | "milestone" | "final" -- domain.contract_obligations.
    # OBLIGATION_KINDS.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "proposed" | "approved" | "committed" | "invoiced" | "paid" |
    # "cancelled" -- domain.contract_obligations.STATUSES. Every write to
    # this column goes through app/contract_obligations.py's
    # `transition_obligation`, which calls domain.contract_obligations.
    # validate_transition first; nothing else in this codebase writes it
    # directly.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # SourceRef, inlined -- same shape as Order/Payment in app/
    # models_ledger.py (C01: "source drill-down is a simple indexed join,
    # not a JSON reach-in"). See class docstring for how this differs from
    # `contract_id` above.
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    locator: Mapped[str] = mapped_column(String(300), nullable=False)
    quoted_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposed_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ObligationLink(Base):
    """One link from a `ContractObligation` to a PO/invoice/payment/credit
    note (TICKET_SPEC.md X0.3). Append-only, like `supplier_payments`/
    `approvals` -- a mistaken link is never edited or deleted, only
    superseded by recording the correction as its own new fact elsewhere
    (same "a changed mind is a new row" discipline as everywhere else in
    this codebase that records a financial fact); migration 0012 REVOKEs
    UPDATE/DELETE on this table for `encore_app`.

    `subject_type`/`subject_id` is deliberately NOT a foreign key (it
    points at `purchase_orders.id`, `supplier_invoices.id` -- for BOTH
    "invoice" and "credit_note", since a credit note is a `SupplierInvoice`
    row with `kind="CREDIT_NOTE"` -- or `supplier_payments.id`/
    `payouts.id` for "payment", depending on whether the obligation's
    counterparty is a supplier or a vendor; no single FK target can express
    that). Same loose, validated-in-application-code pair convention as
    `Case.subject_type`/`subject_id` and `Approval.subject_type`/
    `subject_id` (X0.1) -- see packages/domain/domain.contract_obligations.
    LINK_SUBJECT_TYPES for the exact vocabulary and app/
    contract_obligations.py's `link_obligation` for the one place that
    validates it.

    `amount_cents` here is the PORTION of the linked subject attributed to
    this specific obligation -- not necessarily the subject's own full
    amount (TICKET_SPEC.md's derived-values formula sums these, grouped by
    subject_type, to get invoiced/paid/credited; see
    domain.contract_obligations.compute_obligation_totals).
    """

    __tablename__ = "obligation_links"
    __table_args__ = (
        Index("ix_obligation_links_obligation_id", "obligation_id"),
        Index("ix_obligation_links_subject_type_subject_id", "subject_type", "subject_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    obligation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("contract_obligations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # "po" | "invoice" | "payment" | "credit_note" -- domain.
    # contract_obligations.LINK_SUBJECT_TYPES.
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_capability: Mapped[str] = mapped_column(String(100), nullable=False)
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


# ---------------------------------------------------------------------------
# PO lines, delivery receipts, supplier deposits, supplier quotes (migration
# 0017). No parallel schema: these extend the existing purchase_orders/
# supplier_invoices tables, not a second contract/obligation concept.
# ---------------------------------------------------------------------------


class PurchaseOrderLine(Base):
    """A PO's line items. Optional: a PO created with no `lines` argument
    (every PO before this) has none, and `create_purchase_order`/
    `record_invoice` behave exactly as before for it -- the delivery-
    supported payment limit in app/payables.py only applies when a PO
    actually has lines."""

    __tablename__ = "purchase_order_lines"
    __table_args__ = (UniqueConstraint("po_id", "line_no", name="uq_po_line_no"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    po_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_code: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DeliveryReceipt(Base):
    """One accepted-delivery fact against a PO line. Append-only (REVOKE
    UPDATE/DELETE, migration 0017): a mistaken receipt is never edited,
    only superseded by a new fact elsewhere. `receipt_key` is the fixture's
    stable id (e.g. "late_supplier_r1"), unique, so re-importing the same
    receipt is a no-op, not a duplicate row."""

    __tablename__ = "delivery_receipts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    po_line_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    receipt_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SupplierDeposit(Base):
    """A paid simulated deposit, drawn from the cash pool exactly once
    (`idempotency_key` unique). Append-only."""

    __tablename__ = "supplier_deposits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    po_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    paid_business_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SupplierDepositApplication(Base):
    """One (partial) application of a deposit against an invoice. Append-
    only; UNIQUE(deposit_id, invoice_id) so applying the same deposit to
    the same invoice twice is a no-op at the database level, not just by
    convention."""

    __tablename__ = "supplier_deposit_applications"
    __table_args__ = (UniqueConstraint("deposit_id", "invoice_id", name="uq_deposit_application"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    deposit_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("supplier_deposits.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("supplier_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SupplierQuote(Base):
    """A versioned competing-supplier quote. An unknown price/term stays
    NULL -- never 0 (TICKET: "An unknown price stays NULL; it is never
    0"). Append-only: a revision is a new row with `supersedes_id` set,
    never an edit of the old version."""

    __tablename__ = "supplier_quotes"
    __table_args__ = (
        UniqueConstraint("supplier_id", "request_key", "version", name="uq_supplier_quote_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("supplier_quotes.id", ondelete="SET NULL"), nullable=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    base_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tax_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deposit_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deposit_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    balance_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    insurance_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    citations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
