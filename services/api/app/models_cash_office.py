"""A08 gap closure: organizer cash office, deposit slips and chargeback fees.

New tables only (migration 0010_a08_cash_deposits); nothing existing is
altered.

Why a separate count table: A03's `cash_counts` (app/models_reconciliation.py)
are vendor-DRAWER counts, matched by app/reconciliation.py against the
vendor's VENDOR-RETAINED cash payments. A count made by the ORGANIZER cash
office of CENTRALLY COLLECTED cash is a different custody and is compared
against different payments (central cash), so putting it in `cash_counts`
would make A03 report false variances. `cash_office_counts` holds those.

Chain of custody, each link a row:
  POS central cash (payments) -> cash_office_counts (one bag per
  vendor/night) -> cash_deposit_bags (a bag on a slip, optionally without
  a label) -> cash_deposit_slips -> bank_entries (A03; the bank credit).

All four tables are append-only for encore_app (REVOKE UPDATE/DELETE in
0010). A count, slip or fee notice is never edited: rows are inserted once,
complete. A slip's bank credit is linked either explicitly at recording
time or, when it lands later, by the bank entry whose provider_reference
equals the slip reference -- so the slip row never needs an UPDATE.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class CashOfficeCount(Base):
    """One organizer cash-office count of one vendor's centrally collected
    cash bag for one business date."""

    __tablename__ = "cash_office_counts"
    __table_args__ = (
        UniqueConstraint("festival_id", "bag_reference", name="uq_cash_office_count_bag"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    bag_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    counted_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counted_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    recorded_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashDepositSlip(Base):
    """One bank deposit slip carrying cash-office bags to the bank."""

    __tablename__ = "cash_deposit_slips"
    __table_args__ = (
        UniqueConstraint("festival_id", "slip_reference", name="uq_cash_deposit_slip_ref"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    slip_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    stated_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Explicit link when known at recording time; otherwise resolved later
    # by bank_entries.provider_reference == slip_reference (no UPDATE).
    bank_entry_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bank_entries.id", ondelete="RESTRICT"), nullable=True
    )
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    recorded_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashDepositBag(Base):
    """One bag on a slip. `cash_count_id` is NULL for an unlabelled bag."""

    __tablename__ = "cash_deposit_bags"
    __table_args__ = (UniqueConstraint("slip_id", "bag_reference", name="uq_cash_deposit_bag"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    slip_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("cash_deposit_slips.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bag_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    cash_count_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("cash_office_counts.id", ondelete="RESTRICT"), nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChargebackFee(Base):
    """A processor's fee for one chargeback (e.g. $15). Expensed either
    through the processor batch that withheld it (`bank_entry_id`, whose
    stated batch fee already includes it) or, if no batch states it, by its
    own accrual entry. Recharged to the vendor only where the vendor's
    active contract term permits processing-fee deduction."""

    __tablename__ = "chargeback_fees"
    __table_args__ = (UniqueConstraint("payment_id", name="uq_chargeback_fee_payment"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bank_entry_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bank_entries.id", ondelete="RESTRICT"), nullable=True
    )
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    recorded_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
