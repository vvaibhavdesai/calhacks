"""A03 reconciliation engine.

Matches orders to processor payment records and grouped net bank
settlement entries, plus booth cash counts. Three independent matching
passes, each producing MatchGroup rows (and, for unresolved discrepancies,
Case rows -- reusing A02's `cases` table, per the task instructions):

1. **order_capture** (vendor-scoped): for each order, do its processor
   payment captures sum to the order's charged total? Refunds/chargebacks
   are evaluated against the same order and either explain a variance
   (e.g. a duplicate charge that was refunded) or themselves raise a case
   (e.g. a refund larger than anything ever captured).
2. **cash_count** (vendor-scoped): for each vendor/business_date booth cash
   count, does it match that vendor's vendor-retained cash payments (net of
   any cash refunds) for the same day?
3. **bank_deposit** (festival-scoped, NOT vendor-scoped): for each grouped
   net bank settlement batch, do the centrally-collected payments allocated
   to it sum to the deposit net of any known processor fee? This is
   deliberately festival-level, not vendor-level: a real processor batch
   settlement nets many vendors' transactions into one deposit, so there is
   no vendor to blame for a batch-level shortfall until investigated --
   AGENT_HANDOFF.md's `cases.vendor_id` is nullable precisely to allow this
   ("Case: run_id, vendor_id ...", C03). A pending (not-yet-settled) batch
   never produces a "matched" group -- see classify_bank_deposit.

Key invariant checked throughout (acceptance criterion: "no silent
write-offs"): every classification function below returns exactly one of
`matched`, `pending`, or `case`. There is no fourth "ignore it" outcome.
Variance that isn't fully and specifically explained (by a fee, a refund,
or a settlement-timing window still open) always becomes a case.

Tolerance policy (explicit, documented, per task instructions): this module
uses **zero amount tolerance**. Every amount here is an integer-cents sum
with no rounding/division (percentages are A04's concern, not A03's), so an
exact match is achievable whenever the data is actually consistent; any
non-zero cents of unexplained variance becomes a case rather than being
silently accepted as "close enough".

OPEN QUESTION flagged for the user / C05 signoff (not a business rule this
agent is authorized to invent): how long a centrally-collected payment may
legitimately wait for its bank deposit to settle before that delay itself
becomes a case, rather than a normal "pending" state. CONTRACTS.md's C05
explicitly leaves "payment timing window and matching tolerances" as
"not yet approved/specified" and says "a timing candidate is not confirmed
deposited cash" -- consistent with that, this module treats an unsettled
deposit as `pending` (not matched, not a case) for up to
SETTLEMENT_TIMING_WINDOW_DAYS, and as a case after that. The conservative
default chosen here is 5 calendar days (longer than any ordinary 2-3
business-day card processor settlement cycle, short enough that a genuinely
stuck/missing deposit does not go unnoticed for the length of the whole
three-day festival). This is a placeholder default, not an approved
business rule -- A04/C05 signoff should confirm or replace it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CAPABILITY_RESOLVE_CASE
from app.models_ledger import Case, Order, Payment, Run, Vendor
from app.models_reconciliation import BankEntry, CashCount, MatchGroup, MatchGroupMember
from app.uow import UnitOfWork

# Explicit, documented tolerance (see module docstring): zero. Kept as a
# named constant rather than a bare literal so a future, explicitly-approved
# change is a one-line, reviewable diff, not a hunt through the matching
# functions.
AMOUNT_TOLERANCE_CENTS = 0

# OPEN QUESTION placeholder default -- see module docstring.
SETTLEMENT_TIMING_WINDOW_DAYS = 5


# ---------------------------------------------------------------------------
# Pure classification functions (no DB access -- unit-testable in isolation)
# ---------------------------------------------------------------------------


@dataclass
class PaymentFact:
    id: str
    amount_cents: int
    kind: str  # "capture" | "refund" | "chargeback"


@dataclass
class ClassificationResult:
    """One classification outcome.

    `matched_amount_cents` is the amount this group confirms as genuinely
    matched money -- for an order, its captures NET of every
    refund/chargeback booked against that same order. A04 settles
    commission directly off this figure (app/settlement.py's
    `gather_reconciled_day_facts`), so a refund has to reduce it HERE, at
    the source; recording the refund only in `notes` would leave
    commission assessed on money the customer already got back.
    """

    status: str  # "matched" | "pending" | "case"
    reason_code: str
    variance_cents: int
    matched_amount_cents: int
    notes: str = ""


def classify_order_capture(
    expected_cents: int, payments: list[PaymentFact]
) -> ClassificationResult:
    """Order <-> processor payment capture matching for one order.

    `payments` is every Payment row linked to this order, of any kind.

    `matched_amount_cents` on the returned result is always captures NET
    of refunds/chargebacks -- never the raw capture total -- because that
    figure IS A04's eligible-sales basis for this order. The variance
    classification is a separate question and is still decided from the
    RAW capture total vs the order total (a refund explains a variance;
    it does not make the captures themselves smaller).
    """
    captures = [p for p in payments if p.kind == "capture"]
    adjustments = [p for p in payments if p.kind in ("refund", "chargeback")]
    captures_total = sum(p.amount_cents for p in captures)
    adjustments_total = sum(p.amount_cents for p in adjustments)
    net_captured = captures_total - adjustments_total
    variance = captures_total - expected_cents

    # Sanity check first: refunding/charging back more than was ever
    # captured for this order is always a case, regardless of the raw
    # capture-vs-order variance -- never silently net this to zero.
    if adjustments_total > captures_total:
        return ClassificationResult(
            status="case",
            reason_code="refund_exceeds_captured_amount",
            variance_cents=adjustments_total - captures_total,
            # Nothing is confirmed matched: more was given back than was
            # ever taken. Clamped at zero rather than publishing a
            # negative "matched" amount -- `variance_cents` above carries
            # the size of the overrun, so no information is lost.
            matched_amount_cents=0,
            notes="refund/chargeback total exceeds total captured for this order",
        )

    def _has_duplicate_amount(captures_: list[PaymentFact]) -> bool:
        amounts = [c.amount_cents for c in captures_]
        return len(amounts) >= 2 and len(set(amounts)) < len(amounts)

    if variance == 0:
        if adjustments_total > 0:
            kind_label = (
                "chargeback" if any(p.kind == "chargeback" for p in adjustments) else "refund"
            )
            # The captures match the order total exactly AND money went
            # back to the customer: the order reconciles, but only
            # `net_captured` of it is still eligible sales.
            return ClassificationResult(
                status="matched",
                reason_code=f"{kind_label}_applied",
                variance_cents=0,
                matched_amount_cents=net_captured,
                notes=(
                    f"{kind_label} of {adjustments_total} cents recorded against this order; "
                    f"matched amount is net of it"
                ),
            )
        return ClassificationResult(
            status="matched",
            reason_code="order_payment_match",
            variance_cents=0,
            matched_amount_cents=net_captured,
        )

    if variance > 0 and adjustments_total == variance:
        # The overcharge is exactly explained by a refund/chargeback.
        duplicate_like = _has_duplicate_amount(captures)
        reason = "duplicate_charge_refunded" if duplicate_like else "overcharge_refunded"
        return ClassificationResult(
            status="matched",
            reason_code=reason,
            variance_cents=0,
            # Identical to `expected_cents` here by construction
            # (captures_total - variance), spelled as the net so every
            # branch of this function reports the same quantity.
            matched_amount_cents=net_captured,
            notes=f"overcharge of {variance} cents fully explained by a refund/chargeback",
        )

    if variance > AMOUNT_TOLERANCE_CENTS:
        duplicate_like = _has_duplicate_amount(captures)
        reason = "suspected_duplicate_charge" if duplicate_like else "order_overcharged"
        return ClassificationResult(
            status="case",
            reason_code=reason,
            variance_cents=variance,
            matched_amount_cents=net_captured,
            notes="captured amount exceeds order total and is not (fully) refunded",
        )

    # variance < -AMOUNT_TOLERANCE_CENTS: undercharged / missing payment.
    return ClassificationResult(
        status="case",
        reason_code="missing_or_partial_payment",
        variance_cents=variance,
        matched_amount_cents=net_captured,
        notes="captured amount is less than the order total",
    )


def classify_cash_count(expected_cents: int, counted_cents: int) -> ClassificationResult:
    """Booth cash count <-> vendor-retained cash payments matching."""
    variance = counted_cents - expected_cents
    if abs(variance) <= AMOUNT_TOLERANCE_CENTS:
        return ClassificationResult(
            status="matched",
            reason_code="cash_count_match",
            variance_cents=0,
            matched_amount_cents=counted_cents,
        )
    return ClassificationResult(
        status="case",
        reason_code="cash_variance",
        variance_cents=variance,
        matched_amount_cents=min(counted_cents, expected_cents),
        notes="physical cash count does not match recorded vendor-retained cash payments",
    )


def classify_bank_deposit(
    *,
    captures_total_cents: int,
    entry_amount_cents: int,
    batch_fee_cents: int | None,
    settlement_status: str,
    earliest_payment_business_date: date | None,
    as_of: date,
) -> ClassificationResult:
    """Grouped net bank settlement <-> allocated centrally-collected
    payments matching for one BankEntry batch."""
    fee = batch_fee_cents or 0
    expected_net = captures_total_cents - fee
    variance = entry_amount_cents - expected_net

    if settlement_status != "settled":
        # Pending/in-transit funds are never "matched" -- acceptance
        # criterion: delayed funds do not appear as available/matched cash
        # until actually settled.
        if earliest_payment_business_date is not None:
            days_pending = (as_of - earliest_payment_business_date).days
        else:
            days_pending = 0
        if days_pending > SETTLEMENT_TIMING_WINDOW_DAYS:
            return ClassificationResult(
                status="case",
                reason_code="deposit_settlement_overdue",
                variance_cents=variance,
                matched_amount_cents=0,
                notes=(
                    f"bank deposit has been pending for {days_pending} days, "
                    f"exceeding the {SETTLEMENT_TIMING_WINDOW_DAYS}-day timing window"
                ),
            )
        return ClassificationResult(
            status="pending",
            reason_code="deposit_pending_settlement",
            variance_cents=variance,
            matched_amount_cents=0,
            notes="bank deposit has not yet settled; not counted as matched/available cash",
        )

    if abs(variance) <= AMOUNT_TOLERANCE_CENTS:
        reason = "processor_fee_explained" if fee else "bank_deposit_match"
        return ClassificationResult(
            status="matched",
            reason_code=reason,
            variance_cents=0,
            matched_amount_cents=entry_amount_cents,
            notes=f"fee of {fee} cents accounted for" if fee else "",
        )
    return ClassificationResult(
        status="case",
        reason_code="bank_deposit_variance_unexplained",
        variance_cents=variance,
        matched_amount_cents=captures_total_cents,
        notes="settled bank deposit does not match allocated payments net of known fee",
    )


# ---------------------------------------------------------------------------
# DB-touching orchestration
# ---------------------------------------------------------------------------


@dataclass
class VendorReconciliationResult:
    vendor_id: str
    order_groups: int = 0
    cash_groups: int = 0
    cases_opened: int = 0
    error: str | None = None


@dataclass
class ReconciliationSummary:
    run_id: str
    festival_id: str
    status: str
    vendor_results: list[VendorReconciliationResult] = field(default_factory=list)
    bank_deposit_groups: int = 0
    bank_deposit_cases: int = 0
    total_cases_opened: int = 0


def _open_case(
    db: Session,
    uow: UnitOfWork,
    *,
    run: Run,
    vendor_id: str | None,
    reason_code: str,
    financial_impact_cents: int,
    evidence_refs: list[dict],
    proposed_resolution: str,
    actor_id: str,
    actor_capability: str,
) -> Case:
    case = Case(
        run_id=run.id,
        vendor_id=vendor_id,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        financial_impact_cents=financial_impact_cents,
        proposed_resolution=proposed_resolution,
        required_capability=CAPABILITY_RESOLVE_CASE,
        status="OPEN",
    )
    db.add(case)
    db.flush()

    uow.append_event(
        stream_id=f"case:{case.id}",
        event_type="case.opened",
        aggregate_id=case.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run.id,
        vendor_id=vendor_id,
        payload={
            "reason_code": reason_code,
            "financial_impact_cents": financial_impact_cents,
            "evidence_refs": evidence_refs,
        },
    )
    return case


def _record_match_group(
    db: Session,
    uow: UnitOfWork,
    *,
    run: Run,
    festival_id: str,
    vendor_id: str | None,
    match_type: str,
    business_date_: date | None,
    result: ClassificationResult,
    members: list[tuple[str, str, str, int]],  # (kind, id, role, amount_cents)
    actor_id: str,
    actor_capability: str,
) -> MatchGroup:
    group = MatchGroup(
        run_id=run.id,
        festival_id=festival_id,
        vendor_id=vendor_id,
        match_type=match_type,
        business_date=business_date_,
        matched_amount_cents=result.matched_amount_cents,
        variance_cents=result.variance_cents,
        reason_code=result.reason_code,
        status=result.status,
    )
    db.add(group)
    db.flush()

    for member_kind, member_id, role, amount_cents in members:
        db.add(
            MatchGroupMember(
                match_group_id=group.id,
                member_kind=member_kind,
                member_id=member_id,
                role=role,
                amount_cents=amount_cents,
            )
        )
    db.flush()

    if result.status == "case":
        evidence_refs = [
            {"ref_kind": kind, "ref_id": mid, "role": role} for kind, mid, role, _ in members
        ]
        case = _open_case(
            db,
            uow,
            run=run,
            vendor_id=vendor_id,
            reason_code=result.reason_code,
            financial_impact_cents=result.variance_cents,
            evidence_refs=[{"ref_kind": "match_group", "ref_id": group.id}] + evidence_refs,
            proposed_resolution=result.notes,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        group.case_id = case.id
        db.flush()

    uow.append_event(
        stream_id=f"match_group:{group.id}",
        event_type="reconciliation.match_group.created",
        aggregate_id=group.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run.id,
        vendor_id=vendor_id,
        payload={
            "match_type": match_type,
            "status": result.status,
            "reason_code": result.reason_code,
            "variance_cents": result.variance_cents,
            "matched_amount_cents": result.matched_amount_cents,
            "case_id": group.case_id,
        },
        lineage_refs=[
            {"ref_kind": "match_group", "ref_id": group.id},
        ],
    )
    return group


def reconcile_vendor(
    db: Session,
    uow: UnitOfWork,
    run: Run,
    vendor: Vendor,
    *,
    as_of: date,
    actor_id: str,
    actor_capability: str,
) -> VendorReconciliationResult:
    result = VendorReconciliationResult(vendor_id=vendor.id)

    orders = (
        db.execute(select(Order).where(Order.vendor_id == vendor.id, Order.status == "active"))
        .scalars()
        .all()
    )
    for order in orders:
        payments = db.execute(select(Payment).where(Payment.order_id == order.id)).scalars().all()
        facts = [PaymentFact(id=p.id, amount_cents=p.amount_cents, kind=p.kind) for p in payments]
        classification = classify_order_capture(order.gross_cents, facts)
        members = [("order", order.id, "expected", order.gross_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p in payments
        ]
        _record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="order_capture",
            business_date_=order.business_date,
            result=classification,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        result.order_groups += 1
        if classification.status == "case":
            result.cases_opened += 1

    cash_counts = (
        db.execute(select(CashCount).where(CashCount.vendor_id == vendor.id)).scalars().all()
    )
    for count in cash_counts:
        vendor_payments = db.execute(
            select(Payment, Order.business_date)
            .join(Order, Order.id == Payment.order_id)
            .where(
                Payment.vendor_id == vendor.id,
                Payment.custody == "vendor_retained",
                Order.business_date == count.business_date,
            )
        ).all()
        captures_total = sum(p.amount_cents for p, _bd in vendor_payments if p.kind == "capture")
        refunds_total = sum(
            p.amount_cents for p, _bd in vendor_payments if p.kind in ("refund", "chargeback")
        )
        expected_cents = captures_total - refunds_total
        classification = classify_cash_count(expected_cents, count.counted_cents)
        members = [("cash_count", count.id, "count", count.counted_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p, _bd in vendor_payments
        ]
        _record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="cash_count",
            business_date_=count.business_date,
            result=classification,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        result.cash_groups += 1
        if classification.status == "case":
            result.cases_opened += 1

    return result


def reconcile_bank_deposits(
    db: Session,
    uow: UnitOfWork,
    run: Run,
    festival_id: str,
    *,
    as_of: date,
    actor_id: str,
    actor_capability: str,
) -> tuple[int, int]:
    """Festival-level pass: grouped net bank settlements vs the
    centrally-collected payments allocated to them. Not vendor-scoped --
    see module docstring."""
    entries = (
        db.execute(select(BankEntry).where(BankEntry.festival_id == festival_id)).scalars().all()
    )
    groups_created = 0
    cases_opened = 0
    for entry in entries:
        payments = (
            db.execute(select(Payment).where(Payment.bank_entry_id == entry.id)).scalars().all()
        )
        captures_total = sum(p.amount_cents for p in payments if p.kind == "capture")
        order_dates = (
            db.execute(
                select(Order.business_date)
                .join(Payment, Payment.order_id == Order.id)
                .where(Payment.bank_entry_id == entry.id)
            )
            .scalars()
            .all()
        )
        earliest_date = min(order_dates) if order_dates else entry.business_date
        classification = classify_bank_deposit(
            captures_total_cents=captures_total,
            entry_amount_cents=entry.amount_cents,
            batch_fee_cents=entry.batch_fee_cents,
            settlement_status=entry.settlement_status,
            earliest_payment_business_date=earliest_date,
            as_of=as_of,
        )
        members = [("bank_entry", entry.id, "deposit", entry.amount_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p in payments
        ]
        _record_match_group(
            db,
            uow,
            run=run,
            festival_id=festival_id,
            vendor_id=None,
            match_type="bank_deposit",
            business_date_=entry.business_date,
            result=classification,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        groups_created += 1
        if classification.status == "case":
            cases_opened += 1

    return groups_created, cases_opened


def reconcile_festival(
    db: Session,
    *,
    festival_id: str,
    as_of: date | None = None,
    actor_id: str,
    actor_capability: str,
) -> ReconciliationSummary:
    """Run all three matching passes for one festival.

    Atomicity (C06): this function never calls db.commit() -- the caller
    (the /api/v1/reconciliation/runs endpoint) commits exactly once after
    this returns, so the Run row, every MatchGroup/MatchGroupMember, every
    Case, and every business_event/outbox-adjacent audit trail from this
    invocation land together or not at all.

    Fault isolation (acceptance criterion: "a problem with one vendor does
    not block reconciliation of unrelated vendors"): a genuine data/business
    discrepancy for one vendor's orders or cash counts never raises an
    exception at all -- it is *classified* as a case by the pure functions
    above and processing continues immediately to the next order/vendor. A
    bare `except Exception` around each vendor's pass exists only as a
    backstop against an unexpected bug, so that one vendor's bad luck still
    cannot abort the whole run; it is not how business discrepancies are
    normally handled (those become cases, not caught exceptions).
    """
    if as_of is None:
        as_of = datetime.now(UTC).date()

    run = Run(festival_id=festival_id, status="RUNNING", started_at=datetime.now(UTC))
    db.add(run)
    db.flush()

    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"run:{run.id}",
        event_type="reconciliation.run.started",
        aggregate_id=run.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run.id,
        payload={"festival_id": festival_id, "as_of": as_of.isoformat()},
    )

    vendors = db.execute(select(Vendor).where(Vendor.festival_id == festival_id)).scalars().all()
    vendor_results: list[VendorReconciliationResult] = []
    any_internal_error = False
    for vendor in vendors:
        try:
            vendor_result = reconcile_vendor(
                db,
                uow,
                run,
                vendor,
                as_of=as_of,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        except Exception as exc:  # noqa: BLE001 -- deliberate backstop, see docstring
            any_internal_error = True
            vendor_result = VendorReconciliationResult(vendor_id=vendor.id, error=str(exc))
            _open_case(
                db,
                uow,
                run=run,
                vendor_id=vendor.id,
                reason_code="reconciliation_internal_error",
                financial_impact_cents=0,
                evidence_refs=[],
                proposed_resolution=f"reconciliation raised an unexpected error: {exc}",
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            vendor_result.cases_opened += 1
        vendor_results.append(vendor_result)

    bank_groups, bank_cases = reconcile_bank_deposits(
        db, uow, run, festival_id, as_of=as_of, actor_id=actor_id, actor_capability=actor_capability
    )

    run.status = "PARTIALLY_COMPLETE" if any_internal_error else "COMPLETED"
    run.completed_at = datetime.now(UTC)
    db.flush()

    uow.append_event(
        stream_id=f"run:{run.id}",
        event_type="reconciliation.run.completed",
        aggregate_id=run.id,
        aggregate_version=2,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run.id,
        payload={"status": run.status},
    )

    total_cases = sum(v.cases_opened for v in vendor_results) + bank_cases
    return ReconciliationSummary(
        run_id=run.id,
        festival_id=festival_id,
        status=run.status,
        vendor_results=vendor_results,
        bank_deposit_groups=bank_groups,
        bank_deposit_cases=bank_cases,
        total_cases_opened=total_cases,
    )
