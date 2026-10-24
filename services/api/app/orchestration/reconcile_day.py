"""Day-scoped, incremental driver for A03's reconciliation engine.

`app.reconciliation.reconcile_vendor` matches EVERY order the vendor has
ever had and records a fresh match group (and, for discrepancies, a fresh
OPEN case) each time it runs. That is correct for its one-shot endpoint,
but a nightly close that runs on night 2 (or re-runs after a crash) would
re-open night 1's already-handled cases and give A04's
`gather_reconciled_day_facts` two match groups for the same order.

This module calls the SAME deterministic pieces -- `classify_order_capture`,
`classify_cash_count`, `classify_bank_deposit` and `_record_match_group`
(which opens the case and appends the audit events) -- but only for records
of the run's business date that have no match group yet. It never decides
money itself and never commits; the caller's `run_once` does.

Known limitation (documented in the handoff): a bank deposit already
classified `pending` is not re-evaluated here when it later settles; that
needs A03 to define how a superseding match group replaces a pending one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import reconciliation as a03
from app.models_ledger import Case, Order, Payment, Run, Vendor
from app.models_reconciliation import BankEntry, CashCount, MatchGroup, MatchGroupMember
from app.uow import UnitOfWork


@dataclass
class DayReconciliation:
    order_groups: int = 0
    cash_groups: int = 0
    skipped_already_matched: int = 0
    case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "order_groups": self.order_groups,
            "cash_groups": self.cash_groups,
            "skipped_already_matched": self.skipped_already_matched,
            "case_ids": self.case_ids,
        }


def _already_grouped(db: Session, match_type: str, member_kind: str, member_id: str) -> bool:
    return (
        db.execute(
            select(MatchGroup.id)
            .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
            .where(
                MatchGroup.match_type == match_type,
                MatchGroupMember.member_kind == member_kind,
                MatchGroupMember.member_id == member_id,
            )
        ).first()
        is not None
    )


def reconcile_vendor_day(
    db: Session,
    *,
    run: Run,
    vendor: Vendor,
    business_date: date,
    actor_id: str,
    actor_capability: str,
) -> DayReconciliation:
    uow = UnitOfWork(db)
    out = DayReconciliation()
    orders = (
        db.execute(
            select(Order)
            .where(
                Order.vendor_id == vendor.id,
                Order.status == "active",
                Order.business_date == business_date,
            )
            .order_by(Order.source_native_id)
        )
        .scalars()
        .all()
    )
    for order in orders:
        if _already_grouped(db, "order_capture", "order", order.id):
            out.skipped_already_matched += 1
            continue
        payments = (
            db.execute(select(Payment).where(Payment.order_id == order.id).order_by(Payment.id))
            .scalars()
            .all()
        )
        facts = [
            a03.PaymentFact(id=p.id, amount_cents=p.amount_cents, kind=p.kind) for p in payments
        ]
        result = a03.classify_order_capture(order.gross_cents, facts)
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="order_capture",
            business_date_=order.business_date,
            result=result,
            members=[("order", order.id, "expected", order.gross_cents)]
            + [("payment", p.id, p.kind, p.amount_cents) for p in payments],
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        out.order_groups += 1
        if group.case_id:
            out.case_ids.append(group.case_id)

    counts = (
        db.execute(
            select(CashCount).where(
                CashCount.vendor_id == vendor.id, CashCount.business_date == business_date
            )
        )
        .scalars()
        .all()
    )
    for count in counts:
        if _already_grouped(db, "cash_count", "cash_count", count.id):
            out.skipped_already_matched += 1
            continue
        vendor_payments = (
            db.execute(
                select(Payment)
                .join(Order, Order.id == Payment.order_id)
                .where(
                    Payment.vendor_id == vendor.id,
                    Payment.custody == "vendor_retained",
                    Order.business_date == count.business_date,
                )
            )
            .scalars()
            .all()
        )
        captured = sum(p.amount_cents for p in vendor_payments if p.kind == "capture")
        refunded = sum(
            p.amount_cents for p in vendor_payments if p.kind in ("refund", "chargeback")
        )
        result = a03.classify_cash_count(captured - refunded, count.counted_cents)
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="cash_count",
            business_date_=count.business_date,
            result=result,
            members=[("cash_count", count.id, "count", count.counted_cents)]
            + [("payment", p.id, p.kind, p.amount_cents) for p in vendor_payments],
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        out.cash_groups += 1
        if group.case_id:
            out.case_ids.append(group.case_id)
    return out


def reconcile_bank_deposits_day(
    db: Session,
    *,
    run: Run,
    festival_id: str,
    business_date: date,
    actor_id: str,
    actor_capability: str,
) -> dict:
    """Festival-level pass (not vendor-scoped, same reason as A03): bank
    entries dated on or before the business date that have no bank_deposit
    match group yet."""
    uow = UnitOfWork(db)
    entries = (
        db.execute(
            select(BankEntry)
            .where(BankEntry.festival_id == festival_id, BankEntry.business_date <= business_date)
            .order_by(BankEntry.provider_reference)
        )
        .scalars()
        .all()
    )
    groups, case_ids, skipped = 0, [], 0
    for entry in entries:
        if _already_grouped(db, "bank_deposit", "bank_entry", entry.id):
            skipped += 1
            continue
        payments = (
            db.execute(select(Payment).where(Payment.bank_entry_id == entry.id)).scalars().all()
        )
        order_dates = (
            db.execute(
                select(Order.business_date)
                .join(Payment, Payment.order_id == Order.id)
                .where(Payment.bank_entry_id == entry.id)
            )
            .scalars()
            .all()
        )
        result = a03.classify_bank_deposit(
            captures_total_cents=sum(p.amount_cents for p in payments if p.kind == "capture"),
            entry_amount_cents=entry.amount_cents,
            batch_fee_cents=entry.batch_fee_cents,
            settlement_status=entry.settlement_status,
            earliest_payment_business_date=min(order_dates) if order_dates else entry.business_date,
            as_of=business_date,
        )
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=festival_id,
            vendor_id=None,
            match_type="bank_deposit",
            business_date_=entry.business_date,
            result=result,
            members=[("bank_entry", entry.id, "deposit", entry.amount_cents)]
            + [("payment", p.id, p.kind, p.amount_cents) for p in payments],
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        groups += 1
        if group.case_id:
            case_ids.append(group.case_id)
    return {"bank_deposit_groups": groups, "case_ids": case_ids, "skipped_already_matched": skipped}


def open_cases_for_vendor(db: Session, vendor_id: str) -> list[Case]:
    """The OPEN cases that hold this vendor's money at CONTROLS_CHECKED.
    `Case.blocking` defaults to true, so this is every case kind that has
    ever held a vendor; A06 stage 2's `minimum_period_claim` is the one
    deliberately non-blocking kind -- the signed contract already decides
    the night, so the open question is visible without holding the money
    (the same distinction A07's `release_payout` makes)."""
    return list(
        db.execute(
            select(Case)
            .where(
                Case.vendor_id == vendor_id,
                Case.status == "OPEN",
                Case.blocking.is_(True),
            )
            .order_by(Case.created_at)
        )
        .scalars()
        .all()
    )
