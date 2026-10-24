"""A03 reconciliation schema: grouped net bank settlements, booth cash
counts, and match groups/members that record how orders/payments/bank
deposits/cash counts were (or were not) reconciled.

Scope note, matching the convention in app/models_ledger.py: this is the
reconciliation-specific schema A03's task description asks for. It adds two
new columns to the existing `Payment` model (`kind`, `bank_entry_id`) --
additively, via migration 0004, never touching the already-applied
migrations 0001-0003 -- plus four new tables:

  - BankEntry: one row per grouped/net bank settlement batch (C03 "Bank
    entry": account_id, provider_reference, amount_cents, occurred_at,
    direction, source_refs -- plus A03-specific fields to make the
    many-to-one "many payments, one net deposit minus fees" and the
    pending-vs-settled timing distinction real, not just described).
  - CashCount: one row per vendor/business_date booth cash count, matched
    against that vendor's vendor-retained cash payments for the same day.
  - MatchGroup: one row per reconciliation finding (C03 "Match group":
    member_refs, matched_amounts, variance_cents, reason_code, status,
    source_refs). `match_type` distinguishes which of the three matching
    passes produced it (order_capture / cash_count / bank_deposit).
  - MatchGroupMember: the member_refs themselves, one row per contributing
    order/payment/bank_entry/cash_count, with the role it played (capture,
    refund, chargeback, expected, deposit, count) and the amount it
    contributed. Kept as its own table (not a JSONB blob) for the same
    "source drill-down is a join, not a JSON reach-in" reason A02 chose for
    orders/payments SourceRef fields.

`MatchGroup.case_id` links a match group to the vendor-local (or, for the
festival-level bank-deposit pass, festival-level) Case it produced, when its
status is "case" -- reusing A02's `cases` table unchanged, per the task
instructions ("this task is the first real producer of cases").
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id

# ---------------------------------------------------------------------------
# Grouped net bank settlements
# ---------------------------------------------------------------------------


class BankEntry(Base):
    """One grouped/net bank settlement batch.

    `amount_cents` is the actual net amount the bank recorded (what really
    moved). `batch_gross_cents`/`batch_fee_cents` are optional -- populated
    only when the source processor batch report actually states them, per
    C01's "do not fabricate" spirit: A03 explains a fee it can see in a
    source, it does not invent one to force a match.

    `settlement_status`: "pending" until the bank has actually confirmed the
    deposit, then "settled". This is the concrete mechanism behind the
    acceptance criterion "delayed/in-transit funds do not appear as
    available/matched cash until actually settled" -- a pending BankEntry
    can never produce a match_group with status="matched" (see
    app/reconciliation.py: classify_bank_deposit).
    """

    __tablename__ = "bank_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="credit")
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_gross_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    batch_fee_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settlement_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SourceRef, inlined (C01) -- same shape as Order/Payment.
    source_document_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=True
    )
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locator_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    locator: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quoted_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Booth cash counts
# ---------------------------------------------------------------------------


class CashCount(Base):
    """One physical cash count for one vendor/business_date, matched against
    that vendor's vendor-retained cash payments for the same day."""

    __tablename__ = "cash_counts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    counted_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    counted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    counted_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # SourceRef, inlined (C01).
    source_document_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=True
    )
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locator_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    locator: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quoted_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Match groups / members
# ---------------------------------------------------------------------------


class MatchGroup(Base):
    """One reconciliation finding: a set of member records (orders,
    payments, a bank entry, or a cash count) that were evaluated together,
    plus the resulting classification.

    `vendor_id` is null for the festival-level `bank_deposit` match type
    (a grouped net bank settlement is, by construction, not attributable to
    one vendor -- see app/reconciliation.py docstring); it is always set for
    `order_capture` and `cash_count` match types, which is what makes "a
    problem with one vendor does not block reconciliation of unrelated
    vendors" a meaningful, testable claim.

    `status`: "matched" (variance explained, no case), "pending" (e.g. an
    unsettled bank deposit still within its timing window -- deliberately
    NOT "matched": pending funds are never treated as available/matched
    cash), or "case" (an unresolved discrepancy; `case_id` points at the
    Case this produced).
    """

    __tablename__ = "match_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    match_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    matched_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    variance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="matched")
    case_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MatchGroupMember(Base):
    """One contributing record (order/payment/bank_entry/cash_count) in a
    MatchGroup. `member_id` is a generic reference, not a typed FK (same
    pattern as app.models_ledger.EventLineage.ref_id) because its target
    table varies by `member_kind`."""

    __tablename__ = "match_group_members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    match_group_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("match_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    member_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
