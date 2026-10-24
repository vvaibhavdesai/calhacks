"""A07 controlled payouts: new tables backing release control, the
cumulative daily limit, the simulated rail, and finance-lead approvals.

Scope note, matching the convention in app/models_settlement.py: additive
columns needed on EXISTING tables (`payouts`, `vendors`) are added directly
to their classes in app/models_ledger.py (migration 0007), not duplicated
here. This module holds only the genuinely NEW tables A07 needs:

  - CashPool: a deliberately minimal placeholder for "available organizer
    cash" (A08 owns the real cash/ledger model -- see
    docs/progress/A07-handoff.md's "conservative defaults" section). One
    row per festival_id. `available_cents` is what release's atomic
    reservation decrements; `reserved_cents` is informational (sum of
    money currently reserved-or-spent against releases), kept so a human
    can see "how much is tied up" without summing payouts by hand. This is
    NOT a real GL account and must not be read by A08 as one without that
    task's own deliberate decision to build on it.

  - VendorDayPayoutAggregate: the per-(vendor_id, business_date) row that
    makes the $5,000 cumulative limit race-proof. `committed_cents` is
    incremented, atomically, inside the SAME transaction as a release's
    cash reservation, only once all hard blockers have already passed --
    see app/payout_dispatch.py's `release_payout`. This is the exact same
    "SELECT ... FOR UPDATE on a per-aggregate row inside the same
    transaction as the reservation" pattern app/uow.py's `StreamHead`
    already uses for the hash-chain-under-concurrency problem (A02), reused
    here for a different aggregate.

  - RailSimulatorRecord: the simulated payment rail's OWN record, keyed by
    idempotency_key. PAYMENT_MODE is simulated per C00 -- this is explicitly
    NOT a real payment processor call. Looking this table up BEFORE ever
    writing to it is what makes a resumed/retried dispatch safe (see
    app/rail_simulator.py's module docstring for why this table is
    deliberately NOT updated/committed inside the same transaction as our
    own domain-state changes).

  - PayoutApproval: one row per human decision on a payout (the explicit,
    capability-gated finance-lead-or-ordinary approval that
    AGENT_HANDOFF.md's $5,000/day rule requires above the limit). Append-
    only, like `business_events`/`settlements` -- a decision, once made, is
    never edited; a later re-approval after a version bump is a NEW row.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id

# ---------------------------------------------------------------------------
# Cash pool (minimal placeholder for A08's real cash/ledger model)
# ---------------------------------------------------------------------------


class CashPool(Base):
    __tablename__ = "cash_pools"
    __table_args__ = (UniqueConstraint("festival_id", name="uq_cash_pool_festival"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    available_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Per-vendor/business_date cumulative release aggregate (the $5,000/day
# race-proofing row)
# ---------------------------------------------------------------------------


class VendorDayPayoutAggregate(Base):
    __tablename__ = "vendor_day_payout_aggregates"
    __table_args__ = (
        UniqueConstraint("vendor_id", "business_date", name="uq_vendor_day_payout_aggregate"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Sum of amount_cents for every payout that has successfully passed
    # release's atomic checks for this vendor/day -- incremented exactly
    # once per payout, inside the same transaction as the check, and
    # decremented only if that specific payout's reservation is genuinely
    # released (cancelled/failed BEFORE the simulated rail accepted it --
    # see app/payout_dispatch.py's `_release_reservation`). Never
    # decremented once the rail has accepted the money.
    committed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Simulated payment rail (PAYMENT_MODE=simulated, per C00 -- never a real
# processor call)
# ---------------------------------------------------------------------------


class RailSimulatorRecord(Base):
    """The simulated external rail's own ledger. Deliberately NOT part of
    the append-only business_events/settlements set and NOT written inside
    the same transaction as our own domain-state changes -- see
    app/rail_simulator.py's module docstring for why that separation is the
    entire point (C06: "never claim atomicity across PostgreSQL and
    external provider"). `outcome` may transition DELAYED/UNKNOWN -> a
    final outcome via an explicit reconciliation call
    (app/rail_simulator.py's `resolve_outcome`) -- this table is mutable by
    design, modeling a real external system's record catching up to a
    final state on its own schedule, not our audit trail."""

    __tablename__ = "rail_simulator_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    vendor_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Finance-lead / ordinary payout approvals (append-only decision log)
# ---------------------------------------------------------------------------


class PayoutApproval(Base):
    __tablename__ = "payout_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    payout_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # "approve_payout" (ordinary, within the automatic limit) or
    # "finance_lead" (explicit override above the $5,000/day cumulative
    # limit) -- whichever capability the approving actor actually needed
    # and held, re-read from the database at approval time, never trusted
    # from a client-supplied value (same convention as app/auth.py).
    capability: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
