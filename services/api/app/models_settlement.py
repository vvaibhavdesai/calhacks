"""A04 settlement schema: `settlements` and `settlement_line_items`.

Scope note, matching the convention in app/models_ledger.py and
app/models_reconciliation.py: this is the settlement-specific schema A04's
task description asks for. It adds one additive column to the existing
`Payout` model (`settlement_id`) and two new tables:

  - Settlement: one row per vendor/business_date settlement CALCULATION
    (C03 "Settlement": vendor_id, festival_id, business_date,
    calculation_version, source_manifest_hash, policy_refs, line_items,
    assessed_fee_cents, collected_fee_cents, payout_cents,
    unpaid_balance_cents, holds -- plus the extra distinct quantities A04's
    task brief requires: vendor_fee_receivable_cents, centrally_held_cents,
    vendor_retained_cash_cents, processor_fee_assessed/collected_cents, and
    tax_cents/tips_cents carried for display only).

    Immutability: once a Settlement is written, it is NEVER updated. A
    later recalculation (a late refund, a corrected contract term) inserts
    a NEW Settlement row with `correction_of` pointing at the one it
    corrects -- the original always remains in history exactly as it was
    first computed. This mirrors the existing `JournalEntry.correction_of`
    convention from app/models_ledger.py (A02), not a new pattern. Like
    `business_events`/`source_versions`/`journal_entries`/`journal_lines`,
    `settlements` and `settlement_line_items` get UPDATE/DELETE revoked for
    the `encore_app` application role (see migration 0005) -- this is
    enforced in Postgres, not just "the service layer never calls
    db.execute(update(...))".

  - SettlementLineItem: one row per traceable component of a Settlement
    (eligible_sales / tax / tips / processor_fee / fee_assessed /
    fee_collected / vendor_fee_receivable / unpaid_balance / vendor_payout),
    each carrying its own `source_refs` (a list of SourceRef-shaped dicts,
    or match_group/order/payment ids) so "every line traces back to source
    records" is a real join, not a JSON reach-in -- same "source
    drill-down is a join" reasoning A02/A03 already used for
    orders/payments/match_group_members.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models import _new_id

# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


class Settlement(Base):
    """One vendor/business_date settlement calculation.

    `correction_of`: null for an original calculation; set to the id of the
    Settlement this one corrects when a recalculation happens after a
    prior payout was already confirmed for the vendor (see
    app/settlement.py's `calculate_and_record_settlement`). The original
    row is never mutated -- `status` on the original stays whatever it was
    first written as; a correction's existence is discovered by querying
    `WHERE correction_of = <original.id>`, exactly like
    `JournalEntry.correction_of` already works in app/models_ledger.py.

    `status`: "FINAL" (a normal calculation, not itself a correction) or
    "CORRECTION" (this row's correction_of is set). There is no "DRAFT" in
    A04 -- this task's engine computes a complete result in one
    deterministic pass; A05/A07 are the tasks that layer human review/hold
    workflows and payout execution state on top of a FINAL settlement, and
    should treat FINAL settlements as immutable the same way this task
    does.
    """

    __tablename__ = "settlements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- Frozen calculation inputs (per task: "freeze the calculation:
    # every settlement row must pin the exact calculation-version and
    # contract/policy version used") -----------------------------------
    calculation_version: Mapped[str] = mapped_column(String(50), nullable=False)
    vendor_contract_term_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendor_contract_terms.id", ondelete="SET NULL"), nullable=True
    )
    vendor_contract_term_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commission_bps_used: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_fee_cents_used: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processor_fee_deductible_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_final_period: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Custody-distinct money fields (per task: "Track separately and
    # keep distinct at all times: fee assessed, fee actually collected,
    # vendor-owned/retained cash, funds held centrally by the organizer,
    # and vendor payout payable") ---------------------------------------
    eligible_sales_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    centrally_held_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vendor_retained_cash_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tips_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cumulative_eligible_sales_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_commission_assessed_prior_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    assessed_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collected_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vendor_fee_receivable_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unpaid_balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processor_fee_assessed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processor_fee_collected_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minimum_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Holds / lineage / correction -----------------------------------
    holds: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    policy_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correction_of: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("settlements.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    correction_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="FINAL")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    line_items: Mapped[list[SettlementLineItem]] = relationship(
        back_populates="settlement", cascade="all, delete-orphan"
    )


class SettlementLineItem(Base):
    """One traceable component of a Settlement's math. `line_type` is one
    of: eligible_sales, tax, tips, processor_fee_assessed,
    processor_fee_collected, fee_assessed, fee_collected,
    vendor_fee_receivable, unpaid_balance, vendor_payout.
    `source_refs` holds a list of SourceRef-shaped dicts and/or
    match_group/order/payment/bank_entry ids this component was derived
    from -- same generic-reference pattern already used by
    app.models_ledger.EventLineage.ref_id and
    app.models_reconciliation.MatchGroupMember.member_id."""

    __tablename__ = "settlement_line_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    settlement_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    settlement: Mapped[Settlement] = relationship(back_populates="line_items")
