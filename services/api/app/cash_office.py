"""A08 gap closure: cash office, unmatched bank activity, chargeback fees.

Four things, each posted through `app.accounting.post_cumulative` (so every
posting is idempotent, history-preserving and serialized per fact) and each
writing its audit events through UnitOfWork on the caller's session. Nothing
here commits; the router commits once per request (C06).

1. Cash-office deposits. Centrally collected cash sits in CASH_OFFICE (1050)
   from the sales posting. The organizer cash office counts each vendor's
   bag (`cash_office_counts`), the bags go to the bank on a deposit slip
   (`cash_deposit_slips` / `cash_deposit_bags`), and when the bank credit
   lands (a SETTLED bank entry, linked explicitly or by
   provider_reference == slip_reference) `CASHDEP:<slip>` posts
   Dr BANK_OPERATING / Cr CASH_OFFICE for the amount the bank credited.

2. Count variance. `reconcile_cash_office` compares, per vendor and night,
   the counted cash with the POS centrally collected cash (the same
   per-order split the sales posting uses, so the two cannot drift). A
   difference opens a vendor-scoped CASH_VARIANCE case -- vendor-scoped, so
   A07's payout gate holds only that vendor. A vendor/night with POS cash
   but no count opens MISSING_CASH_COUNT, citing any unlabelled bag on that
   night's slips; an unlabelled bag no vendor/night explains opens a
   festival-level MISSING_CASH_COUNT. A slip whose bank credit differs from
   its bags opens CASH_DEPOSIT_VARIANCE. Nothing is ever written off
   automatically: CASH_OVER_SHORT is posted only by `decide_case`, a
   finance-lead command that records the human decision on the case.

3. Unmatched bank entries. `scan_bank_entries` books every settled bank
   entry that no known record explains to SUSPENSE (1999) and opens a
   festival-level UNMATCHED_BANK_ENTRY case. A record that later explains
   it reverses the suspense posting with a linked #adj entry. The human
   decision BOOK_AS_BANK_FEE reclassifies SUSPENSE -> OPERATING_EXPENSE
   with a separate entry whose `correction_of` is the suspense entry; the
   suspense entry is never rewritten.

4. Chargeback fees. The processor's chargeback fee is processor fee
   expense: through the processor batch that withheld it when that batch's
   stated fee includes it (already posted by `DEPOSIT:<bank entry>`), or by
   its own accrual entry otherwise. It is recharged to the vendor
   (Dr VENDOR_FUNDS_PAYABLE / Cr PROCESSOR_FEE_EXPENSE) only when the
   vendor's active contract term (A04 `resolve_vendor_terms`) has
   `processor_fee_deductible`. The recharge is cumulative, so re-running
   post-day after terms change adds or reverses it as a linked adjustment.

Cases are A05 `cases` rows. Each carries a `{"ref_kind": "festival"}`
evidence ref (role "scope") so festival-level cases (vendor_id NULL) can be
listed per festival, and one `role: "subject"` ref that makes opening it
idempotent.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime

from domain import accounting as acct
from domain import cash as cash_rules
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.accounting import (
    BusinessDateClosedError,
    PostResult,
    _bank_ref,
    _family,
    _net_by_account,
    post_cumulative,
)
from app.models import CAPABILITY_FINANCE_LEAD, _new_id
from app.models_cash_office import (
    CashDepositBag,
    CashDepositSlip,
    CashOfficeCount,
    ChargebackFee,
)
from app.models_ledger import Case, JournalEntry, Order, Payment, Vendor
from app.models_reconciliation import BankEntry
from app.uow import UnitOfWork

CASH_VARIANCE = "CASH_VARIANCE"
MISSING_CASH_COUNT = "MISSING_CASH_COUNT"
CASH_DEPOSIT_VARIANCE = "CASH_DEPOSIT_VARIANCE"
UNMATCHED_BANK_ENTRY = "UNMATCHED_BANK_ENTRY"
CASH_CASE_REASONS = (CASH_VARIANCE, MISSING_CASH_COUNT, CASH_DEPOSIT_VARIANCE)

ALLOWED_DECISIONS = {
    CASH_VARIANCE: ("ORGANIZER_ABSORBS_SHORTAGE", "BOOK_OVERAGE", "CHARGE_TO_VENDOR"),
    CASH_DEPOSIT_VARIANCE: ("ORGANIZER_ABSORBS_SHORTAGE", "BOOK_OVERAGE"),
    MISSING_CASH_COUNT: ("ACCEPT_POS_AND_BANK_DEPOSIT_AS_EVIDENCE",),
    UNMATCHED_BANK_ENTRY: ("BOOK_AS_BANK_FEE",),
}

# Journal kinds that book a bank entry to/out of suspense. They never count
# as an "explanation" of that bank entry.
_SUSPENSE_KINDS = ("UNMATCHED_BANK_ENTRY", "BANK_FEE_RECLASS")


class CashOfficeConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


class CashOfficeRefusedError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


class CashOfficeNotFoundError(LookupError):
    pass


def _lock(db: Session, key: str) -> None:
    # The new tables are append-only for encore_app, and SELECT ... FOR
    # UPDATE needs UPDATE privilege, so writers serialize on a
    # transaction-scoped advisory lock instead.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _event(db, *, stream, event_type, aggregate_id, actor_id, actor_capability, payload, **kw):
    UnitOfWork(db).append_event(
        stream_id=stream,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_version=kw.pop("version", 1),
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload=payload,
        **kw,
    )


def signed_bank_cents(be: BankEntry) -> int:
    return be.amount_cents if be.direction == "credit" else -be.amount_cents


def _vendor_in_festival(db: Session, vendor_id: str, festival_id: str) -> Vendor:
    v = db.get(Vendor, vendor_id)
    if v is None or v.festival_id != festival_id:
        raise CashOfficeNotFoundError(f"vendor {vendor_id} not found in {festival_id}")
    return v


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _scope(festival_id: str) -> dict:
    return {"ref_kind": "festival", "ref_id": festival_id, "role": "scope"}


def _subject(kind: str, ref_id: str) -> dict:
    return {"ref_kind": kind, "ref_id": ref_id, "role": "subject"}


def _subject_of(case: Case) -> dict | None:
    return next((r for r in case.evidence_refs or [] if r.get("role") == "subject"), None)


def find_case(db: Session, reason_code: str, subject: dict) -> Case | None:
    return (
        db.execute(
            select(Case)
            .where(Case.reason_code == reason_code, Case.evidence_refs.contains([subject]))
            .order_by(Case.created_at)
        )
        .scalars()
        .first()
    )


def _ensure_case(
    db: Session,
    *,
    festival_id: str,
    reason_code: str,
    vendor_id: str | None,
    business_date: date | None,
    subject: dict,
    evidence: list[dict],
    impact_cents: int,
    proposed: str,
    actor_id: str | None,
    actor_capability: str | None,
) -> tuple[Case, bool]:
    existing = find_case(db, reason_code, subject)
    if existing is not None:
        if existing.status == "OPEN" and existing.financial_impact_cents != impact_cents:
            existing.financial_impact_cents = impact_cents
            existing.version += 1
            _event(
                db,
                stream=f"case:{existing.id}",
                event_type="case.impact_updated",
                aggregate_id=existing.id,
                version=existing.version,
                actor_id=actor_id,
                actor_capability=actor_capability,
                vendor_id=existing.vendor_id,
                payload={"financial_impact_cents": impact_cents},
            )
        return existing, False
    case = Case(
        vendor_id=vendor_id,
        reason_code=reason_code,
        business_date=business_date,
        evidence_refs=[subject, _scope(festival_id), *evidence],
        financial_impact_cents=impact_cents,
        proposed_resolution=proposed,
        required_capability=CAPABILITY_FINANCE_LEAD,
        status="OPEN",
    )
    db.add(case)
    db.flush()
    _event(
        db,
        stream=f"case:{case.id}",
        event_type="case.opened",
        aggregate_id=case.id,
        actor_id=actor_id,
        actor_capability=actor_capability,
        vendor_id=vendor_id,
        payload={
            "reason_code": reason_code,
            "financial_impact_cents": impact_cents,
            "evidence_refs": case.evidence_refs,
        },
    )
    return case, True


def festival_cases(db: Session, festival_id: str, reasons, *, status: str | None = None):
    q = select(Case).where(
        Case.reason_code.in_(reasons), Case.evidence_refs.contains([_scope(festival_id)])
    )
    if status is not None:
        q = q.where(Case.status == status)
    return db.execute(q.order_by(Case.created_at)).scalars().all()


def case_out(c: Case) -> dict:
    return {
        "case_id": c.id,
        "reason_code": c.reason_code,
        "vendor_id": c.vendor_id,
        "business_date": c.business_date.isoformat() if c.business_date else None,
        "status": c.status,
        "financial_impact_cents": c.financial_impact_cents,
        "subject": _subject_of(c),
        "required_capability": c.required_capability,
        "resolution_decision": c.resolution_decision,
        "resolution_effect": c.resolution_effect,
    }


# ---------------------------------------------------------------------------
# 1. Counts and deposit slips
# ---------------------------------------------------------------------------


def record_cash_count(
    db: Session,
    *,
    festival_id: str,
    vendor_id: str,
    business_date: date,
    bag_reference: str,
    counted_cents: int,
    counted_at: datetime | None,
    counted_by: str | None,
    source_refs: list,
    count_id: str | None,
    actor_id: str | None,
    actor_capability: str | None,
) -> tuple[CashOfficeCount, bool]:
    if counted_cents < 0:
        raise CashOfficeRefusedError("negative_count")
    _vendor_in_festival(db, vendor_id, festival_id)
    _lock(db, f"cash_office_count:{festival_id}:{bag_reference}")
    existing = db.execute(
        select(CashOfficeCount).where(
            CashOfficeCount.festival_id == festival_id,
            CashOfficeCount.bag_reference == bag_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.vendor_id, existing.business_date, existing.counted_cents) != (
            vendor_id,
            business_date,
            counted_cents,
        ):
            raise CashOfficeConflictError(
                "cash_count_conflict",
                {"existing_id": existing.id, "counted_cents": existing.counted_cents},
            )
        return existing, False
    if count_id is not None and db.get(CashOfficeCount, count_id) is not None:
        raise CashOfficeConflictError("cash_count_id_in_use", {"id": count_id})
    row = CashOfficeCount(
        id=count_id or _new_id(),
        festival_id=festival_id,
        vendor_id=vendor_id,
        business_date=business_date,
        bag_reference=bag_reference,
        counted_cents=counted_cents,
        counted_at=counted_at,
        counted_by=counted_by,
        source_refs=source_refs,
        recorded_by=actor_id,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        stream=f"cash_office:{festival_id}",
        event_type="cash_office.count_recorded",
        aggregate_id=row.id,
        actor_id=actor_id,
        actor_capability=actor_capability,
        vendor_id=vendor_id,
        payload={
            "bag_reference": bag_reference,
            "business_date": business_date.isoformat(),
            "counted_cents": counted_cents,
        },
    )
    return row, True


def slip_bank_entry(db: Session, slip: CashDepositSlip) -> BankEntry | None:
    """The bank credit for a slip: the explicit link, else the festival's
    credit whose provider_reference is the slip reference."""
    if slip.bank_entry_id:
        return db.get(BankEntry, slip.bank_entry_id)
    return (
        db.execute(
            select(BankEntry)
            .where(
                BankEntry.festival_id == slip.festival_id,
                BankEntry.provider_reference == slip.slip_reference,
                BankEntry.direction == "credit",
            )
            .order_by(BankEntry.occurred_at)
        )
        .scalars()
        .first()
    )


def _bags(db: Session, slip_id: str) -> list[CashDepositBag]:
    return list(
        db.execute(
            select(CashDepositBag)
            .where(CashDepositBag.slip_id == slip_id)
            .order_by(CashDepositBag.bag_reference)
        )
        .scalars()
        .all()
    )


def record_deposit_slip(
    db: Session,
    *,
    festival_id: str,
    slip_reference: str,
    business_date: date,
    total_cents: int,
    bank_entry_id: str | None,
    bags: list[dict],
    source_refs: list,
    slip_id: str | None,
    actor_id: str | None,
    actor_capability: str | None,
) -> tuple[CashDepositSlip, bool, dict]:
    if not bags:
        raise CashOfficeRefusedError("slip_without_bags")
    refs = [b["bag_reference"] for b in bags]
    if len(refs) != len(set(refs)):
        raise CashOfficeRefusedError("duplicate_bag_reference_on_slip")
    _lock(db, f"cash_deposit_slip:{festival_id}:{slip_reference}")
    wanted = sorted((b["bag_reference"], b.get("cash_count_id"), b["amount_cents"]) for b in bags)
    existing = db.execute(
        select(CashDepositSlip).where(
            CashDepositSlip.festival_id == festival_id,
            CashDepositSlip.slip_reference == slip_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        have = sorted(
            (b.bag_reference, b.cash_count_id, b.amount_cents) for b in _bags(db, existing.id)
        )
        if (
            existing.business_date,
            existing.stated_total_cents,
            existing.bank_entry_id,
            have,
        ) != (business_date, total_cents, bank_entry_id, wanted):
            raise CashOfficeConflictError("deposit_slip_conflict", {"existing_id": existing.id})
        return existing, False, _deposit_posting_status(db, existing)

    if bank_entry_id is not None:
        be = db.get(BankEntry, bank_entry_id)
        if be is None or be.festival_id != festival_id:
            raise CashOfficeNotFoundError(f"bank entry {bank_entry_id} not found")
        if be.direction != "credit":
            raise CashOfficeRefusedError("bank_entry_not_a_credit", {"bank_entry_id": be.id})
    for b in bags:
        cid = b.get("cash_count_id")
        if cid is None:
            continue
        cnt = db.get(CashOfficeCount, cid)
        if cnt is None or cnt.festival_id != festival_id:
            raise CashOfficeNotFoundError(f"cash count {cid} not found")
        dup = db.execute(
            select(CashDepositBag.id).where(CashDepositBag.cash_count_id == cid)
        ).first()
        if dup is not None:
            raise CashOfficeConflictError("cash_count_already_deposited", {"cash_count_id": cid})
    if slip_id is not None and db.get(CashDepositSlip, slip_id) is not None:
        raise CashOfficeConflictError("deposit_slip_id_in_use", {"id": slip_id})

    slip = CashDepositSlip(
        id=slip_id or _new_id(),
        festival_id=festival_id,
        slip_reference=slip_reference,
        business_date=business_date,
        stated_total_cents=total_cents,
        bank_entry_id=bank_entry_id,
        source_refs=source_refs,
        recorded_by=actor_id,
    )
    db.add(slip)
    db.flush()
    for b in bags:
        db.add(
            CashDepositBag(
                slip_id=slip.id,
                bag_reference=b["bag_reference"],
                cash_count_id=b.get("cash_count_id"),
                amount_cents=b["amount_cents"],
            )
        )
    db.flush()
    _event(
        db,
        stream=f"cash_office:{festival_id}",
        event_type="cash_office.deposit_slip_recorded",
        aggregate_id=slip.id,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "slip_reference": slip_reference,
            "business_date": business_date.isoformat(),
            "stated_total_cents": total_cents,
            "bags": [list(w) for w in wanted],
            "bank_entry_id": bank_entry_id,
        },
    )
    try:
        post_cash_deposits(
            db,
            festival_id=festival_id,
            slip_ids=[slip.id],
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
    except BusinessDateClosedError as exc:
        return (
            slip,
            True,
            {"status": "REFUSED_BUSINESS_DATE_CLOSED", "business_date": str(exc.business_date)},
        )
    return slip, True, _deposit_posting_status(db, slip)


def _deposit_posting_status(db: Session, slip: CashDepositSlip) -> dict:
    be = slip_bank_entry(db, slip)
    fam = _family(db, f"CASHDEP:{slip.id}")
    if fam:
        return {
            "status": "POSTED",
            "entry_keys": [e.entry_key for e in fam],
            "bank_entry_id": be.id if be else None,
        }
    if be is None:
        return {"status": "AWAITING_BANK_CREDIT", "bank_entry_id": None}
    if be.settlement_status != "settled":
        return {"status": "BANK_CREDIT_PENDING", "bank_entry_id": be.id}
    return {"status": "NOT_POSTED", "bank_entry_id": be.id}


def post_cash_deposits(
    db: Session,
    *,
    festival_id: str,
    business_date: date | None = None,
    slip_ids: list[str] | None = None,
    actor_id: str | None,
    actor_capability: str | None,
) -> list[PostResult]:
    """`CASHDEP:<slip>` for every slip whose bank credit has SETTLED (on the
    bank entry's business date): Dr bank / Cr cash office for what the bank
    actually credited. A difference against the bags stays in the cash
    office and is surfaced as CASH_DEPOSIT_VARIANCE, never written off."""
    q = select(CashDepositSlip).where(CashDepositSlip.festival_id == festival_id)
    if slip_ids is not None:
        q = q.where(CashDepositSlip.id.in_(slip_ids))
    results = []
    for slip in db.execute(q.order_by(CashDepositSlip.business_date)).scalars():
        be = slip_bank_entry(db, slip)
        if be is None or be.settlement_status != "settled":
            continue
        if business_date is not None and be.business_date != business_date:
            continue
        results.append(
            post_cumulative(
                db,
                festival_id=festival_id,
                base_key=f"CASHDEP:{slip.id}",
                business_date=be.business_date,
                kind="CASH_OFFICE_DEPOSIT",
                lines=acct.cash_office_deposit(signed_bank_cents(be)),
                source_refs=[_bank_ref(be), *(slip.source_refs or [])],
                source_record_ids=[be.id, slip.id],
                note=f"Cash-office deposit slip {slip.slip_reference}.",
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 2. Count variance
# ---------------------------------------------------------------------------


def central_cash_expected(
    db: Session, festival_id: str, business_date: date
) -> dict[str, tuple[int, list[str]]]:
    """POS centrally collected CASH per vendor for the night: the same
    per-order split and asset choice (first payment's method) that
    `post_sales_for_day` uses for its CASH_OFFICE line, so the expected
    amount here is exactly what the sales posting debited to 1050."""
    rows = db.execute(
        select(Payment, Order)
        .join(Order, Payment.order_id == Order.id)
        .join(Vendor, Vendor.id == Payment.vendor_id)
        .where(
            Vendor.festival_id == festival_id,
            Payment.custody == "centrally_collected",
            Order.business_date == business_date,
        )
        .order_by(Payment.created_at, Payment.id)
    ).all()
    by_order: dict[str, list[Payment]] = defaultdict(list)
    orders: dict[str, Order] = {}
    for p, o in rows:
        by_order[o.id].append(p)
        orders[o.id] = o
    out: dict[str, list] = defaultdict(lambda: [0, []])
    for oid, payments in by_order.items():
        if payments[0].method.lower() in ("card", "wristband"):
            continue
        captures = sum(abs(p.amount_cents) for p in payments if p.kind == "capture")
        reversals = sum(abs(p.amount_cents) for p in payments if p.kind != "capture")
        m = acct.split_order_money(
            order_gross_cents=orders[oid].gross_cents,
            captures_cents=captures,
            reversals_cents=reversals,
        )
        row = out[orders[oid].vendor_id]
        row[0] += m.gross_net_cents
        row[1].extend(p.id for p in payments)
    return {v: (amt, ids) for v, (amt, ids) in out.items()}


def _counts_for(db: Session, festival_id: str, business_date: date) -> list[CashOfficeCount]:
    return list(
        db.execute(
            select(CashOfficeCount)
            .where(
                CashOfficeCount.festival_id == festival_id,
                CashOfficeCount.business_date == business_date,
            )
            .order_by(CashOfficeCount.bag_reference)
        )
        .scalars()
        .all()
    )


def _slips_for(db: Session, festival_id: str, business_date: date) -> list[CashDepositSlip]:
    return list(
        db.execute(
            select(CashDepositSlip)
            .where(
                CashDepositSlip.festival_id == festival_id,
                CashDepositSlip.business_date == business_date,
            )
            .order_by(CashDepositSlip.slip_reference)
        )
        .scalars()
        .all()
    )


def _vendor_day(vendor_id: str, business_date: date) -> str:
    return f"{vendor_id}:{business_date.isoformat()}"


def vendor_day_variance(
    db: Session, festival_id: str, vendor_id: str, business_date: date
) -> cash_rules.CountCheck:
    counted = sum(
        c.counted_cents
        for c in _counts_for(db, festival_id, business_date)
        if c.vendor_id == vendor_id
    )
    expected = central_cash_expected(db, festival_id, business_date).get(vendor_id, (0, []))[0]
    return cash_rules.CountCheck(counted_cents=counted, expected_cents=expected)


def slip_check(db: Session, slip: CashDepositSlip) -> cash_rules.SlipCheck:
    bags = _bags(db, slip.id)
    be = slip_bank_entry(db, slip)
    mismatches = 0
    for b in bags:
        if b.cash_count_id:
            cnt = db.get(CashOfficeCount, b.cash_count_id)
            if cnt is not None and cnt.counted_cents != b.amount_cents:
                mismatches += 1
    return cash_rules.SlipCheck(
        bag_total_cents=sum(b.amount_cents for b in bags),
        stated_total_cents=slip.stated_total_cents,
        bank_cents=(
            signed_bank_cents(be) if be is not None and be.settlement_status == "settled" else None
        ),
        unlabelled_bag_count=sum(1 for b in bags if b.cash_count_id is None),
        bag_count_mismatches=mismatches,
    )


def _ref(kind: str, rid: str, role: str) -> dict:
    return {"ref_kind": kind, "ref_id": rid, "role": role}


def reconcile_cash_office(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    actor_id: str | None,
    actor_capability: str | None,
) -> dict:
    """Counts vs POS central cash, missing counts, unlabelled bags and slip
    vs bank, for one night. Opens (idempotently) the cases described in the
    module docstring. Posts nothing."""
    _lock(db, f"cash_office_reconcile:{festival_id}:{business_date.isoformat()}")
    expected = central_cash_expected(db, festival_id, business_date)
    counts = _counts_for(db, festival_id, business_date)
    counted: dict[str, list[CashOfficeCount]] = defaultdict(list)
    for c in counts:
        counted[c.vendor_id].append(c)
    slips = _slips_for(db, festival_id, business_date)
    unlabelled = [(s, b) for s in slips for b in _bags(db, s.id) if b.cash_count_id is None]
    kw = dict(festival_id=festival_id, actor_id=actor_id, actor_capability=actor_capability)

    vendors_out = []
    missing_vendor_cases = []
    for vendor_id in sorted(set(expected) | set(counted)):
        exp, payment_ids = expected.get(vendor_id, (0, []))
        rows = counted.get(vendor_id, [])
        subject = _subject("cash_office_vendor_day", _vendor_day(vendor_id, business_date))
        row = {
            "vendor_id": vendor_id,
            "expected_pos_cash_cents": exp,
            "counted_cents": None,
            "variance_cents": None,
            "status": None,
            "cash_count_ids": [c.id for c in rows],
            "case_id": None,
        }
        if not rows:
            if exp == 0:
                continue
            evidence = [_ref("payment", pid, "pos_cash") for pid in payment_ids[:50]] + [
                _ref("cash_deposit_bag", b.id, "candidate_unlabelled_bag") for _s, b in unlabelled
            ]
            case, _ = _ensure_case(
                db,
                reason_code=MISSING_CASH_COUNT,
                vendor_id=vendor_id,
                business_date=business_date,
                subject=subject,
                evidence=evidence,
                impact_cents=exp,
                proposed=(
                    "POS shows centrally collected cash but the cash office recorded no count "
                    "for this vendor/night. Decide ACCEPT_POS_AND_BANK_DEPOSIT_AS_EVIDENCE once "
                    "the deposit (e.g. an unlabelled bag) is shown to carry it."
                ),
                **kw,
            )
            missing_vendor_cases.append(case)
            row.update(status="MISSING_CASH_COUNT", case_id=case.id)
            vendors_out.append(row)
            continue
        check = cash_rules.CountCheck(
            counted_cents=sum(c.counted_cents for c in rows), expected_cents=exp
        )
        row.update(
            counted_cents=check.counted_cents,
            variance_cents=check.variance_cents,
            status=check.status,
        )
        if check.variance_cents:
            case, _ = _ensure_case(
                db,
                reason_code=CASH_VARIANCE,
                vendor_id=vendor_id,
                business_date=business_date,
                subject=subject,
                evidence=[_ref("cash_office_count", c.id, "count") for c in rows]
                + [_ref("payment", pid, "pos_cash") for pid in payment_ids[:50]],
                impact_cents=check.variance_cents,
                proposed=(
                    "Cash-office count differs from POS cash. Holds only this vendor. Decide "
                    "ORGANIZER_ABSORBS_SHORTAGE / CHARGE_TO_VENDOR (shortage) or BOOK_OVERAGE. "
                    "Never written off automatically."
                ),
                **kw,
            )
            row["case_id"] = case.id
        vendors_out.append(row)

    bag_cases = []
    if unlabelled and not missing_vendor_cases:
        for s, b in unlabelled:
            case, _ = _ensure_case(
                db,
                reason_code=MISSING_CASH_COUNT,
                vendor_id=None,
                business_date=business_date,
                subject=_subject("cash_deposit_bag", b.id),
                evidence=[_ref("cash_deposit_slip", s.id, "slip")],
                impact_cents=b.amount_cents,
                proposed=(
                    "An unlabelled bag was deposited and no vendor/night lacks a count. "
                    "Identify the bag before accepting it."
                ),
                **kw,
            )
            bag_cases.append(case.id)

    slips_out = []
    for s in slips:
        chk = slip_check(db, s)
        be = slip_bank_entry(db, s)
        row = {
            "slip_id": s.id,
            "slip_reference": s.slip_reference,
            "bag_total_cents": chk.bag_total_cents,
            "stated_total_cents": chk.stated_total_cents,
            "bank_entry_id": be.id if be else None,
            "bank_cents": chk.bank_cents,
            "bank_variance_cents": chk.bank_variance_cents,
            "unlabelled_bag_count": chk.unlabelled_bag_count,
            "bag_count_mismatches": chk.bag_count_mismatches,
            "status": chk.status,
            "posting": _deposit_posting_status(db, s),
            "case_id": None,
        }
        if chk.bank_cents is not None and (
            chk.bank_variance_cents
            or chk.bag_count_mismatches
            or chk.stated_total_cents != chk.bag_total_cents
        ):
            case, _ = _ensure_case(
                db,
                reason_code=CASH_DEPOSIT_VARIANCE,
                vendor_id=None,
                business_date=business_date,
                subject=_subject("cash_deposit_slip", s.id),
                evidence=[_ref("bank_entry", be.id, "bank_credit")],
                impact_cents=chk.bank_variance_cents or 0,
                proposed=(
                    "Bank credit, slip total, bags and counts disagree. Decide "
                    "ORGANIZER_ABSORBS_SHORTAGE or BOOK_OVERAGE for the bank difference."
                ),
                **kw,
            )
            row["case_id"] = case.id
        slips_out.append(row)

    return {
        "festival_id": festival_id,
        "business_date": business_date.isoformat(),
        "vendors": vendors_out,
        "slips": slips_out,
        "unlabelled_bags": [
            {
                "slip_id": s.id,
                "bag_id": b.id,
                "bag_reference": b.bag_reference,
                "amount_cents": b.amount_cents,
            }
            for s, b in unlabelled
        ],
        "unlabelled_bag_case_ids": bag_cases,
        "open_case_ids": [
            c.id
            for c in festival_cases(db, festival_id, CASH_CASE_REASONS, status="OPEN")
            if c.business_date == business_date
        ],
    }


# ---------------------------------------------------------------------------
# 3. Unmatched bank entries
# ---------------------------------------------------------------------------


def _explained_bank_entry_ids(db: Session, festival_id: str) -> dict[str, str]:
    """bank entry id -> what explains it, from every known record."""
    out: dict[str, str] = {}
    for (bid,) in db.execute(
        select(BankEntry.id).where(
            BankEntry.festival_id == festival_id, BankEntry.batch_gross_cents.is_not(None)
        )
    ):
        out[bid] = "processor_deposit"
    for (bid,) in db.execute(
        select(Payment.bank_entry_id)
        .join(BankEntry, BankEntry.id == Payment.bank_entry_id)
        .where(BankEntry.festival_id == festival_id)
        .distinct()
    ):
        out.setdefault(bid, "payments")
    for slip in db.execute(
        select(CashDepositSlip).where(CashDepositSlip.festival_id == festival_id)
    ).scalars():
        be = slip_bank_entry(db, slip)
        if be is not None:
            out.setdefault(be.id, "cash_deposit_slip")
    bank_ids = {
        bid
        for (bid,) in db.execute(select(BankEntry.id).where(BankEntry.festival_id == festival_id))
    }
    for kind, ids in db.execute(
        select(JournalEntry.kind, JournalEntry.source_record_ids).where(
            JournalEntry.festival_id == festival_id, JournalEntry.kind.not_in(_SUSPENSE_KINDS)
        )
    ):
        for rid in ids or []:
            if rid in bank_ids:
                out.setdefault(rid, f"journal:{kind}")
    return out


def _family_net(db: Session, key: str) -> tuple[list[JournalEntry], dict[str, int]]:
    fam = _family(db, key)
    return fam, {k: v for k, v in _net_by_account(fam).items() if v}


def _settled_entries(db: Session, festival_id: str, *, on=None, through=None) -> list[BankEntry]:
    q = select(BankEntry).where(
        BankEntry.festival_id == festival_id, BankEntry.settlement_status == "settled"
    )
    if on is not None:
        q = q.where(BankEntry.business_date == on)
    if through is not None:
        q = q.where(BankEntry.business_date <= through)
    return list(db.execute(q.order_by(BankEntry.occurred_at, BankEntry.id)).scalars().all())


def unaccounted_bank_entries(db: Session, festival_id: str, *, through: date) -> list[str]:
    """Settled bank entries that are neither explained by a record nor in
    suspense: bank activity missing from the books (a hard close check)."""
    explained = _explained_bank_entry_ids(db, festival_id)
    return [
        be.id
        for be in _settled_entries(db, festival_id, through=through)
        if be.id not in explained and not _family(db, f"BANK_UNMATCHED:{be.id}")
    ]


def scan_bank_entries(
    db: Session,
    *,
    festival_id: str,
    business_date: date | None,
    actor_id: str | None,
    actor_capability: str | None,
) -> dict:
    _lock(db, f"bank_scan:{festival_id}")
    explained = _explained_bank_entry_ids(db, festival_id)
    rows = []
    for be in _settled_entries(db, festival_id, on=business_date):
        key = f"BANK_UNMATCHED:{be.id}"
        fam, net = _family_net(db, key)
        reclassed = bool(_family(db, f"BANK_FEE_RECLASS:{be.id}"))
        subject = _subject("bank_entry", be.id)
        row = {
            "bank_entry_id": be.id,
            "provider_reference": be.provider_reference,
            "business_date": be.business_date.isoformat(),
            "signed_amount_cents": signed_bank_cents(be),
            "explained_by": explained.get(be.id),
            "status": None,
            "case_id": None,
            "entry_keys": [e.entry_key for e in fam],
        }
        if be.id in explained:
            if net and not reclassed:
                r = post_cumulative(
                    db,
                    festival_id=festival_id,
                    base_key=key,
                    business_date=be.business_date,
                    kind="UNMATCHED_BANK_ENTRY",
                    lines=[],
                    source_record_ids=[be.id],
                    note=f"Explained by {explained[be.id]}; suspense reversed.",
                    actor_id=actor_id,
                    actor_capability=actor_capability,
                )
                case = find_case(db, UNMATCHED_BANK_ENTRY, subject)
                if case is not None and case.status == "OPEN":
                    _resolve(
                        db,
                        case,
                        decision="EXPLAINED_BY_RECORD",
                        note=f"Explained by {explained[be.id]}; no write-off.",
                        effect={"reversal_entry_key": r.entry.entry_key if r.entry else None},
                        actor_id=None,
                        actor_capability=actor_capability,
                    )
                row.update(status="EXPLAINED_SUSPENSE_REVERSED", case_id=case.id if case else None)
            elif reclassed:
                row["status"] = "EXPLAINED_AFTER_HUMAN_DECISION_NEEDS_REVIEW"
            else:
                row["status"] = "EXPLAINED"
            rows.append(row)
            continue
        status = "IN_SUSPENSE" if not reclassed else "RECLASSIFIED"
        if not fam:
            try:
                post_cumulative(
                    db,
                    festival_id=festival_id,
                    base_key=key,
                    business_date=be.business_date,
                    kind="UNMATCHED_BANK_ENTRY",
                    lines=acct.unmatched_bank_entry(signed_bank_cents(be)),
                    source_refs=[_bank_ref(be)],
                    source_record_ids=[be.id],
                    note="Held in suspense pending review; not written off.",
                    actor_id=actor_id,
                    actor_capability=actor_capability,
                )
            except BusinessDateClosedError:
                status = "UNMATCHED_NOT_POSTED_BUSINESS_DATE_CLOSED"
        case, _ = _ensure_case(
            db,
            festival_id=festival_id,
            reason_code=UNMATCHED_BANK_ENTRY,
            vendor_id=None,
            business_date=be.business_date,
            subject=subject,
            evidence=[],
            impact_cents=signed_bank_cents(be),
            proposed=(
                "Bank activity no known record explains; held in SUSPENSE (1999). Decide "
                "BOOK_AS_BANK_FEE to reclassify, or record the explaining record and re-scan."
            ),
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        row.update(
            status=status,
            case_id=case.id,
            entry_keys=[e.entry_key for e in _family(db, key)],
        )
        rows.append(row)
    return {
        "festival_id": festival_id,
        "business_date": business_date.isoformat() if business_date else None,
        "bank_entries": rows,
        "unmatched_count": sum(1 for r in rows if r["explained_by"] is None),
    }


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------


def _resolve(db, case: Case, *, decision, note, effect, actor_id, actor_capability) -> None:
    case.status = "RESOLVED"
    case.resolution_decision = decision
    case.resolution_note = note
    case.resolution_effect = effect
    case.resolved_by = actor_id
    case.resolved_capability = actor_capability if actor_id else None
    case.resolved_at = datetime.now(UTC)
    case.version += 1
    db.flush()
    _event(
        db,
        stream=f"case:{case.id}",
        event_type="case.resolved",
        aggregate_id=case.id,
        version=case.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        vendor_id=case.vendor_id,
        payload={"decision": decision, "resolution_note": note, "resolution_effect": effect},
        lineage_refs=[{"ref_kind": "case", "ref_id": case.id}],
    )


def decide_case(
    db: Session,
    *,
    case_id: str,
    decision: str,
    note: str | None,
    business_date: date | None,
    actor_id: str,
    actor_capability: str,
) -> tuple[Case, dict]:
    """Records a finance lead's decision on a cash-office / bank case and
    posts exactly what that decision means. The amount is recomputed live
    from the records at decision time, not copied from the case."""
    case = db.execute(select(Case).where(Case.id == case_id).with_for_update()).scalar_one_or_none()
    if case is None:
        raise CashOfficeNotFoundError(f"case {case_id} not found")
    if case.reason_code not in ALLOWED_DECISIONS:
        raise CashOfficeRefusedError("not_a_cash_office_case", {"reason_code": case.reason_code})
    if case.status != "OPEN":
        raise CashOfficeConflictError("case_not_open", {"status": case.status})
    if decision not in ALLOWED_DECISIONS[case.reason_code]:
        raise CashOfficeRefusedError(
            "decision_not_allowed", {"allowed": list(ALLOWED_DECISIONS[case.reason_code])}
        )
    subject = _subject_of(case) or {}
    scope = next((r for r in case.evidence_refs if r.get("role") == "scope"), {})
    festival_id = scope.get("ref_id")
    effect: dict = {"decision": decision}
    post_kw = dict(festival_id=festival_id, actor_id=actor_id, actor_capability=actor_capability)

    if case.reason_code in (CASH_VARIANCE, CASH_DEPOSIT_VARIANCE):
        if case.reason_code == CASH_VARIANCE:
            vendor_id, d = subject["ref_id"].rsplit(":", 1)
            variance = vendor_day_variance(
                db, festival_id, vendor_id, date.fromisoformat(d)
            ).variance_cents
            refs = [subject["ref_id"]]
        else:
            slip = db.get(CashDepositSlip, subject["ref_id"])
            variance = slip_check(db, slip).bank_variance_cents or 0
            refs = [slip.id]
        if variance == 0:
            raise CashOfficeRefusedError("no_variance_to_decide")
        if (decision == "BOOK_OVERAGE") != (variance > 0):
            raise CashOfficeRefusedError(
                "decision_does_not_match_variance", {"variance_cents": variance}
            )
        lines = (
            acct.cash_shortage_to_vendor(variance)
            if decision == "CHARGE_TO_VENDOR"
            else acct.cash_over_short(variance)
        )
        r = post_cumulative(
            db,
            base_key=f"CASH_OVER_SHORT:{case.id}",
            business_date=business_date or case.business_date,
            kind="CASH_OVER_SHORT",
            lines=lines,
            source_record_ids=refs + [case.id],
            note=f"Human decision {decision} on case {case.id}.",
            **post_kw,
        )
        effect.update(
            variance_cents=variance, journal_entry_key=r.entry.entry_key if r.entry else None
        )
    elif case.reason_code == UNMATCHED_BANK_ENTRY:
        be = db.get(BankEntry, subject["ref_id"])
        _fam, net = _family_net(db, f"BANK_UNMATCHED:{be.id}")
        if not net:
            raise CashOfficeRefusedError("bank_entry_not_in_suspense", {"bank_entry_id": be.id})
        r = post_cumulative(
            db,
            base_key=f"BANK_FEE_RECLASS:{be.id}",
            business_date=business_date or be.business_date,
            kind="BANK_FEE_RECLASS",
            lines=acct.suspense_reclass(signed_bank_cents(be), "5400"),
            source_refs=[_bank_ref(be)],
            source_record_ids=[be.id, case.id],
            correction_of_key=f"BANK_UNMATCHED:{be.id}",
            note=f"Human decision {decision} on case {case.id}: suspense -> operating expense.",
            **post_kw,
        )
        effect.update(
            amount_cents=-signed_bank_cents(be),
            journal_entry_key=r.entry.entry_key if r.entry else None,
        )
    else:  # MISSING_CASH_COUNT: evidence accepted, nothing to post
        effect["journal_entry_key"] = None

    _resolve(
        db,
        case,
        decision=decision,
        note=note,
        effect=effect,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    return case, effect


# ---------------------------------------------------------------------------
# 4. Chargeback fees
# ---------------------------------------------------------------------------


def record_chargeback_fee(
    db: Session,
    *,
    payment_id: str,
    fee_cents: int,
    business_date: date,
    bank_entry_id: str | None,
    source_refs: list,
    actor_id: str | None,
    actor_capability: str | None,
) -> tuple[ChargebackFee, bool, dict]:
    if fee_cents <= 0:
        raise CashOfficeRefusedError("fee_must_be_positive")
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise CashOfficeNotFoundError(f"payment {payment_id} not found")
    if payment.kind != "chargeback":
        raise CashOfficeRefusedError("not_a_chargeback", {"kind": payment.kind})
    vendor = db.get(Vendor, payment.vendor_id)
    festival_id = vendor.festival_id
    _lock(db, f"chargeback_fee:{payment_id}")
    existing = db.execute(
        select(ChargebackFee).where(ChargebackFee.payment_id == payment_id)
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.fee_cents, existing.business_date, existing.bank_entry_id) != (
            fee_cents,
            business_date,
            bank_entry_id,
        ):
            raise CashOfficeConflictError("chargeback_fee_conflict", {"existing_id": existing.id})
        return existing, False, chargeback_fee_status(db, existing)
    if bank_entry_id is not None:
        be = db.get(BankEntry, bank_entry_id)
        if be is None or be.festival_id != festival_id:
            raise CashOfficeNotFoundError(f"bank entry {bank_entry_id} not found")
        if be.batch_fee_cents is None or be.batch_fee_cents < fee_cents:
            raise CashOfficeConflictError(
                "fee_not_in_batch_fees",
                {"bank_entry_id": be.id, "batch_fee_cents": be.batch_fee_cents},
            )
    row = ChargebackFee(
        festival_id=festival_id,
        payment_id=payment_id,
        vendor_id=vendor.id,
        business_date=business_date,
        fee_cents=fee_cents,
        bank_entry_id=bank_entry_id,
        source_refs=source_refs,
        recorded_by=actor_id,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        stream=f"cash_office:{festival_id}",
        event_type="chargeback_fee.recorded",
        aggregate_id=row.id,
        actor_id=actor_id,
        actor_capability=actor_capability,
        vendor_id=vendor.id,
        payload={
            "payment_id": payment_id,
            "fee_cents": fee_cents,
            "business_date": business_date.isoformat(),
            "bank_entry_id": bank_entry_id,
        },
    )
    post_chargeback_fee(db, row, actor_id=actor_id, actor_capability=actor_capability)
    return row, True, chargeback_fee_status(db, row)


def _recharge_permitted(db: Session, row: ChargebackFee) -> tuple[bool, str | None]:
    from app.settlement import resolve_vendor_terms  # A04; imported lazily

    _bps, _min, deductible, term = resolve_vendor_terms(db, row.vendor_id, row.business_date)
    return bool(deductible), term.id if term is not None else None


def post_chargeback_fee(
    db: Session, row: ChargebackFee, *, actor_id: str | None, actor_capability: str | None
) -> list[PostResult]:
    kw = dict(
        festival_id=row.festival_id,
        business_date=row.business_date,
        source_refs=row.source_refs,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    results = []
    if row.bank_entry_id is None:
        results.append(
            post_cumulative(
                db,
                base_key=f"CHARGEBACK_FEE:{row.payment_id}",
                kind="CHARGEBACK_FEE",
                lines=acct.chargeback_fee_accrual(row.fee_cents),
                source_record_ids=[row.id, row.payment_id],
                note="Chargeback fee not inside any processor batch's stated fees.",
                **kw,
            )
        )
    permitted, term_id = _recharge_permitted(db, row)
    results.append(
        post_cumulative(
            db,
            base_key=f"CHARGEBACK_FEE_RECHARGE:{row.payment_id}",
            kind="PROCESSOR_FEE_RECHARGE",
            lines=acct.processor_fee_recharge(row.fee_cents) if permitted else [],
            source_record_ids=[row.id, row.payment_id] + ([term_id] if term_id else []),
            note=(
                "Recharged: vendor contract term permits processing-fee deduction."
                if permitted
                else "Recharge withdrawn: vendor contract term no longer permits it."
            ),
            **kw,
        )
    )
    return results


def chargeback_fee_status(db: Session, row: ChargebackFee) -> dict:
    permitted, term_id = _recharge_permitted(db, row)
    _f, net = _family_net(db, f"CHARGEBACK_FEE_RECHARGE:{row.payment_id}")
    return {
        "id": row.id,
        "payment_id": row.payment_id,
        "vendor_id": row.vendor_id,
        "business_date": row.business_date.isoformat(),
        "fee_cents": row.fee_cents,
        "expense_booked_via": (
            f"DEPOSIT:{row.bank_entry_id}"
            if row.bank_entry_id
            else f"CHARGEBACK_FEE:{row.payment_id}"
        ),
        "recharge_permitted_by_contract": permitted,
        "contract_term_id": term_id,
        "recharged_cents": net.get("2000", 0),
    }


def post_chargeback_fees_for_day(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    actor_id: str | None,
    actor_capability: str | None,
) -> list[PostResult]:
    """Re-evaluates (cumulatively) every chargeback fee dated on the day, so
    a contract term activated later adds -- or a withdrawn one reverses --
    the recharge as a linked adjustment."""
    results: list[PostResult] = []
    for row in db.execute(
        select(ChargebackFee).where(
            ChargebackFee.festival_id == festival_id, ChargebackFee.business_date == business_date
        )
    ).scalars():
        results.extend(
            post_chargeback_fee(db, row, actor_id=actor_id, actor_capability=actor_capability)
        )
    return results


# ---------------------------------------------------------------------------
# Reads for reports / close
# ---------------------------------------------------------------------------


def unposted_cash_deposit_slips(db: Session, festival_id: str, *, through: date) -> list[str]:
    out = []
    for slip in db.execute(
        select(CashDepositSlip).where(CashDepositSlip.festival_id == festival_id)
    ).scalars():
        be = slip_bank_entry(db, slip)
        if (
            be is not None
            and be.settlement_status == "settled"
            and be.business_date <= through
            and not _family(db, f"CASHDEP:{slip.id}")
        ):
            out.append(slip.id)
    return out


def cash_office_summary(db: Session, festival_id: str) -> dict:
    slips = []
    for slip in db.execute(
        select(CashDepositSlip)
        .where(CashDepositSlip.festival_id == festival_id)
        .order_by(CashDepositSlip.business_date, CashDepositSlip.slip_reference)
    ).scalars():
        chk = slip_check(db, slip)
        slips.append(
            {
                "slip_id": slip.id,
                "slip_reference": slip.slip_reference,
                "business_date": slip.business_date.isoformat(),
                "bag_total_cents": chk.bag_total_cents,
                "bank_cents": chk.bank_cents,
                "status": chk.status,
                "posting": _deposit_posting_status(db, slip),
            }
        )
    open_cases = [
        case_out(c)
        for c in festival_cases(
            db, festival_id, (*CASH_CASE_REASONS, UNMATCHED_BANK_ENTRY), status="OPEN"
        )
    ]
    return {
        "deposit_slips": slips,
        "deposited_cents": sum(
            s["bank_cents"] or 0 for s in slips if s["posting"]["status"] == "POSTED"
        ),
        "awaiting_bank_credit_cents": sum(
            s["bag_total_cents"] for s in slips if s["posting"]["status"] != "POSTED"
        ),
        "open_cases": open_cases,
        "note": (
            "Cash-office cash is pending (not spendable) until the bank credit lands. Variances "
            "and unmatched bank activity are held as cases; nothing is written off without a "
            "recorded finance-lead decision."
        ),
    }


def suspense_items(db: Session, festival_id: str) -> list[dict]:
    rows = []
    for e in db.execute(
        select(JournalEntry)
        .where(
            JournalEntry.festival_id == festival_id,
            JournalEntry.entry_key.like("BANK_UNMATCHED:%"),
            JournalEntry.correction_of.is_(None),
        )
        .order_by(JournalEntry.business_date, JournalEntry.entry_key)
    ).scalars():
        be_id = e.entry_key.split(":", 1)[1]
        _f, net = _family_net(db, e.entry_key)
        _r, reclass = _family_net(db, f"BANK_FEE_RECLASS:{be_id}")
        held = net.get("1999", 0) + reclass.get("1999", 0)
        case = find_case(db, UNMATCHED_BANK_ENTRY, _subject("bank_entry", be_id))
        rows.append(
            {
                "bank_entry_id": be_id,
                "business_date": e.business_date.isoformat(),
                "suspense_cents": held,
                "status": "IN_SUSPENSE" if held else ("RECLASSIFIED" if reclass else "REVERSED"),
                "case_id": case.id if case else None,
                "case_status": case.status if case else None,
            }
        )
    return rows
