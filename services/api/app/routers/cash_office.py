"""A08 gap-closure endpoints: cash-office counts and deposit slips, count
reconciliation, unmatched bank entries, chargeback fees and the human
decisions on the cases they open.

Capabilities (A01 `require_capability`): `close_books` records counts,
slips and fee notices and runs the reconcile/scan commands -- the same
capability that posts the books. `finance_lead` is required for every
decision that books a variance or reclassifies suspense: those are
judgement calls, never automatic. Reads require any authenticated user.
One commit per request; audit events are appended on the same session.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.accounting as accounting
import app.cash_office as co
from app.auth import get_current_user, require_capability
from app.db import get_db
from app.models import CAPABILITY_CLOSE_BOOKS, CAPABILITY_FINANCE_LEAD, User
from app.models_cash_office import CashDepositSlip, CashOfficeCount, ChargebackFee

router = APIRouter(prefix="/api/v1/finance", tags=["cash-office"])

_ERRORS = (
    co.CashOfficeConflictError,
    co.CashOfficeRefusedError,
    co.CashOfficeNotFoundError,
    accounting.BusinessDateClosedError,
)


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, co.CashOfficeNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, co.CashOfficeConflictError):
        return HTTPException(status_code=409, detail={"code": exc.code, "details": exc.details})
    if isinstance(exc, co.CashOfficeRefusedError):
        return HTTPException(status_code=422, detail={"code": exc.code, "details": exc.details})
    if isinstance(exc, accounting.BusinessDateClosedError):
        return HTTPException(
            status_code=409,
            detail={"code": "business_date_closed", "business_date": str(exc.business_date)},
        )
    raise exc


# Instants are `str`: the handlers emit `.isoformat()` (offset `+00:00`),
# and a `datetime` field would make pydantic rewrite that to `...Z`.
#
# Every route below sets `response_model_exclude_unset`. Two shapes here
# genuinely omit keys rather than sending them null -- a deposit posting
# that has not posted carries no `entry_keys`, and a case resolution's
# `resolution_effect` carries different keys per decision -- and without
# `exclude_unset` FastAPI would serialise the model defaults and add keys
# these responses have never had.


class PostingStatusOut(BaseModel):
    """Whether a slip's cash has reached the books
    (app/cash_office.py `_deposit_posting_status`)."""

    # POSTED | AWAITING_BANK_CREDIT | BANK_CREDIT_PENDING | NOT_POSTED
    status: str
    # Null until the slip's bank credit is known.
    bank_entry_id: str | None
    # Sent ONLY when status is POSTED; absent (not null) otherwise.
    entry_keys: list[str] = []


class CaseSubjectOut(BaseModel):
    ref_kind: str
    ref_id: str
    role: str


class CashCaseOut(BaseModel):
    """A cash-office or bank case (app/cash_office.py `case_out`). Also the
    element type of `open_cases` on the cash-office view."""

    case_id: str
    reason_code: str
    vendor_id: str | None
    business_date: str | None
    status: str
    financial_impact_cents: int
    # Null when the case does not name one record.
    subject: CaseSubjectOut | None
    required_capability: str | None
    # Null while the case is still OPEN.
    resolution_decision: str | None
    # What the recorded decision did. The keys depend on the decision
    # (`variance_cents` + `journal_entry_key` for an over/short,
    # `reversal_entry_key` for a reversed suspense), so this stays an open
    # object rather than a model that would drop the other branches.
    resolution_effect: dict | None


class CashCaseListOut(BaseModel):
    items: list[CashCaseOut]
    # Always null: one festival's cash cases are a page, not a stream.
    next_cursor: str | None


class CashOfficeCountOut(BaseModel):
    """A cash-office BAG count. Named for its router because
    `records.CashCountOut` is a different thing -- a POS till count, with
    `amount_cents` and a custody -- and two models under one name mangle
    the generated client.
    """

    id: str
    festival_id: str
    vendor_id: str
    business_date: str
    bag_reference: str
    counted_cents: int
    counted_at: str | None
    counted_by: str | None
    source_refs: list


class RecordCountOut(BaseModel):
    count: CashOfficeCountOut
    # False when an identical count was already recorded (idempotent replay).
    created: bool


class SlipSummaryOut(BaseModel):
    """A slip as the cash-office view lists it."""

    slip_id: str
    slip_reference: str
    business_date: str
    bag_total_cents: int
    # Null until a bank credit is matched to the slip.
    bank_cents: int | None
    status: str
    posting: PostingStatusOut


class SuspenseItemOut(BaseModel):
    """A bank entry held in suspense because no record explains it."""

    bank_entry_id: str
    business_date: str
    suspense_cents: int
    status: str  # IN_SUSPENSE | RECLASSIFIED | REVERSED
    case_id: str | None
    case_status: str | None


class CashOfficeOut(BaseModel):
    deposit_slips: list[SlipSummaryOut]
    deposited_cents: int
    awaiting_bank_credit_cents: int
    open_cases: list[CashCaseOut]
    counts: list[CashOfficeCountOut]
    suspense: list[SuspenseItemOut]
    note: str


class RecordSlipOut(BaseModel):
    slip_id: str
    slip_reference: str
    created: bool
    bag_total_cents: int
    unlabelled_bag_count: int
    check_status: str
    posting: PostingStatusOut


class SlipBagOut(BaseModel):
    id: str
    bag_reference: str
    # Null for an unlabelled bag -- no count claims it.
    cash_count_id: str | None
    amount_cents: int


class SlipDetailOut(BaseModel):
    slip_id: str
    slip_reference: str
    business_date: str
    stated_total_cents: int
    bags: list[SlipBagOut]
    bag_total_cents: int
    # Both null until the slip's bank credit is known.
    bank_cents: int | None
    bank_variance_cents: int | None
    check_status: str
    posting: PostingStatusOut
    source_refs: list


class ReconcileVendorOut(BaseModel):
    vendor_id: str
    expected_pos_cash_cents: int
    # All three stay null when the cash office recorded NO count for this
    # vendor-night: there is nothing to compare, which is itself the finding.
    counted_cents: int | None
    variance_cents: int | None
    status: str | None
    cash_count_ids: list[str]
    case_id: str | None


class ReconcileSlipOut(BaseModel):
    slip_id: str
    slip_reference: str
    bag_total_cents: int
    stated_total_cents: int
    bank_entry_id: str | None
    bank_cents: int | None
    bank_variance_cents: int | None
    unlabelled_bag_count: int
    bag_count_mismatches: int
    status: str
    posting: PostingStatusOut
    case_id: str | None


class UnlabelledBagOut(BaseModel):
    slip_id: str
    bag_id: str
    bag_reference: str
    amount_cents: int


class ReconcileOut(BaseModel):
    festival_id: str
    business_date: str
    vendors: list[ReconcileVendorOut]
    slips: list[ReconcileSlipOut]
    unlabelled_bags: list[UnlabelledBagOut]
    unlabelled_bag_case_ids: list[str]
    open_case_ids: list[str]


class ScanBankEntryOut(BaseModel):
    bank_entry_id: str
    provider_reference: str | None
    business_date: str
    # Signed: a credit is positive, a debit negative.
    signed_amount_cents: int
    # The record that accounts for this entry, or null if nothing does --
    # which is what makes it unmatched.
    explained_by: str | None
    status: str | None
    case_id: str | None
    entry_keys: list[str]


class ScanOut(BaseModel):
    festival_id: str
    # Null when the scan covered every settled date, not just one night.
    business_date: str | None
    bank_entries: list[ScanBankEntryOut]
    unmatched_count: int


class ChargebackFeeStatusOut(BaseModel):
    id: str
    payment_id: str
    vendor_id: str
    business_date: str
    fee_cents: int
    # Whether the vendor's terms allow recharging the fee, and how much of
    # it was. Null `contract_term_id` means no term was in force to consult.
    recharge_permitted_by_contract: bool
    recharged_cents: int
    contract_term_id: str | None
    expense_booked_via: str


class ChargebackFeeOut(BaseModel):
    chargeback_fee: ChargebackFeeStatusOut
    created: bool


class ChargebackFeeListOut(BaseModel):
    items: list[ChargebackFeeStatusOut]
    next_cursor: str | None


class DecisionOut(BaseModel):
    case: CashCaseOut
    # What the decision booked. Keys vary by decision, as on
    # `CashCaseOut.resolution_effect`.
    effect: dict


def _count_out(c: CashOfficeCount) -> dict:
    return {
        "id": c.id,
        "festival_id": c.festival_id,
        "vendor_id": c.vendor_id,
        "business_date": c.business_date.isoformat(),
        "bag_reference": c.bag_reference,
        "counted_cents": c.counted_cents,
        "counted_at": c.counted_at.isoformat() if c.counted_at else None,
        "counted_by": c.counted_by,
        "source_refs": c.source_refs,
    }


# ---------------------------------------------------------------------------
# Counts / slips
# ---------------------------------------------------------------------------


class CashCountIn(BaseModel):
    festival_id: str
    vendor_id: str
    business_date: date
    bag_reference: str = Field(min_length=1, max_length=200)
    counted_cents: int
    counted_at: datetime | None = None
    counted_by: str | None = None
    source_refs: list = Field(default_factory=list)
    id: str | None = Field(default=None, max_length=32)


@router.post(
    "/cash-office/counts",
    status_code=201,
    response_model=RecordCountOut,
    response_model_exclude_unset=True,
)
def record_count(
    body: CashCountIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        row, created = co.record_cash_count(
            db,
            festival_id=body.festival_id,
            vendor_id=body.vendor_id,
            business_date=body.business_date,
            bag_reference=body.bag_reference,
            counted_cents=body.counted_cents,
            counted_at=body.counted_at,
            counted_by=body.counted_by,
            source_refs=body.source_refs,
            count_id=body.id,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return {"count": _count_out(row), "created": created}


class BagIn(BaseModel):
    bag_reference: str = Field(min_length=1, max_length=200)
    amount_cents: int
    cash_count_id: str | None = None  # None = an unlabelled bag


class SlipIn(BaseModel):
    festival_id: str
    slip_reference: str = Field(min_length=1, max_length=200)
    business_date: date
    total_cents: int
    bank_entry_id: str | None = None
    bags: list[BagIn]
    source_refs: list = Field(default_factory=list)
    id: str | None = Field(default=None, max_length=32)


@router.post(
    "/cash-office/deposit-slips",
    status_code=201,
    response_model=RecordSlipOut,
    response_model_exclude_unset=True,
)
def record_slip(
    body: SlipIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    """Records a slip and its bags. If the slip's bank credit has already
    settled, `CASHDEP:<slip>` is posted in the same transaction; otherwise
    post-day for the bank credit's date posts it when it lands."""
    try:
        slip, created, posting = co.record_deposit_slip(
            db,
            festival_id=body.festival_id,
            slip_reference=body.slip_reference,
            business_date=body.business_date,
            total_cents=body.total_cents,
            bank_entry_id=body.bank_entry_id,
            bags=[b.model_dump() for b in body.bags],
            source_refs=body.source_refs,
            slip_id=body.id,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    chk = co.slip_check(db, slip)
    return {
        "slip_id": slip.id,
        "slip_reference": slip.slip_reference,
        "created": created,
        "bag_total_cents": chk.bag_total_cents,
        "unlabelled_bag_count": chk.unlabelled_bag_count,
        "check_status": chk.status,
        "posting": posting,
    }


@router.get("/cash-office", response_model=CashOfficeOut, response_model_exclude_unset=True)
def get_cash_office(
    festival_id: str,
    business_date: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Live cash-office view: counts, slips (with bank and posting status)
    and open cash/bank cases. Read-only."""
    q = select(CashOfficeCount).where(CashOfficeCount.festival_id == festival_id)
    if business_date is not None:
        q = q.where(CashOfficeCount.business_date == business_date)
    counts = db.execute(
        q.order_by(CashOfficeCount.business_date, CashOfficeCount.bag_reference)
    ).scalars()
    out = co.cash_office_summary(db, festival_id)
    if business_date is not None:
        out["deposit_slips"] = [
            s for s in out["deposit_slips"] if s["business_date"] == business_date.isoformat()
        ]
    out["counts"] = [_count_out(c) for c in counts]
    out["suspense"] = co.suspense_items(db, festival_id)
    return out


class DayIn(BaseModel):
    festival_id: str
    business_date: date


@router.post(
    "/cash-office/reconcile", response_model=ReconcileOut, response_model_exclude_unset=True
)
def reconcile(
    body: DayIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    """Counts vs POS central cash, missing counts, unlabelled bags and slip
    vs bank for one night; opens cases idempotently. Posts nothing."""
    try:
        out = co.reconcile_cash_office(
            db,
            festival_id=body.festival_id,
            business_date=body.business_date,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return out


# ---------------------------------------------------------------------------
# Bank entries
# ---------------------------------------------------------------------------


class ScanIn(BaseModel):
    festival_id: str
    business_date: date | None = None


@router.post("/bank-entries/scan", response_model=ScanOut, response_model_exclude_unset=True)
def scan(
    body: ScanIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    """Books every settled bank entry no known record explains to SUSPENSE
    and opens an UNMATCHED_BANK_ENTRY case; reverses suspense for one a
    record now explains. Idempotent."""
    try:
        out = co.scan_bank_entries(
            db,
            festival_id=body.festival_id,
            business_date=body.business_date,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return out


# ---------------------------------------------------------------------------
# Chargeback fees
# ---------------------------------------------------------------------------


class ChargebackFeeIn(BaseModel):
    payment_id: str
    fee_cents: int
    business_date: date
    bank_entry_id: str | None = None  # the processor batch deposit that withheld it
    source_refs: list = Field(default_factory=list)


@router.post("/chargeback-fees", status_code=201, response_model=ChargebackFeeOut)
def record_chargeback_fee(
    body: ChargebackFeeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        row, created, status = co.record_chargeback_fee(
            db,
            **body.model_dump(),
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return {"chargeback_fee": status, "created": created}


@router.get("/chargeback-fees", response_model=ChargebackFeeListOut)
def list_chargeback_fees(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(
        select(ChargebackFee)
        .where(ChargebackFee.festival_id == festival_id)
        .order_by(ChargebackFee.business_date, ChargebackFee.payment_id)
    ).scalars()
    return {"items": [co.chargeback_fee_status(db, r) for r in rows], "next_cursor": None}


# ---------------------------------------------------------------------------
# Human decisions on cash-office / bank cases
# ---------------------------------------------------------------------------


class DecisionIn(BaseModel):
    decision: str
    note: str | None = None
    # Posting date for any entry the decision books; defaults to the case's
    # night (or the bank entry's date). Pass an open date if that is closed.
    business_date: date | None = None


@router.post(
    "/cash-cases/{case_id}/decision",
    response_model=DecisionOut,
    response_model_exclude_unset=True,
)
def decide(
    case_id: str,
    body: DecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_FINANCE_LEAD)),
) -> dict:
    try:
        case, effect = co.decide_case(
            db,
            case_id=case_id,
            decision=body.decision,
            note=body.note,
            business_date=body.business_date,
            actor_id=user.id,
            actor_capability=CAPABILITY_FINANCE_LEAD,
        )
    except _ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return {"case": co.case_out(case), "effect": effect}


@router.get("/cash-cases", response_model=CashCaseListOut, response_model_exclude_unset=True)
def list_cash_cases(
    festival_id: str,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    rows = co.festival_cases(
        db, festival_id, (*co.CASH_CASE_REASONS, co.UNMATCHED_BANK_ENTRY), status=status
    )
    return {"items": [co.case_out(c) for c in rows], "next_cursor": None}


@router.get(
    "/cash-office/deposit-slips/{slip_id}",
    response_model=SlipDetailOut,
    response_model_exclude_unset=True,
)
def get_slip(
    slip_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    slip = db.get(CashDepositSlip, slip_id)
    if slip is None:
        raise HTTPException(status_code=404, detail="deposit slip not found")
    chk = co.slip_check(db, slip)
    return {
        "slip_id": slip.id,
        "slip_reference": slip.slip_reference,
        "business_date": slip.business_date.isoformat(),
        "stated_total_cents": slip.stated_total_cents,
        "bags": [
            {
                "id": b.id,
                "bag_reference": b.bag_reference,
                "cash_count_id": b.cash_count_id,
                "amount_cents": b.amount_cents,
            }
            for b in co._bags(db, slip.id)
        ],
        "bag_total_cents": chk.bag_total_cents,
        "bank_cents": chk.bank_cents,
        "bank_variance_cents": chk.bank_variance_cents,
        "check_status": chk.status,
        "posting": co._deposit_posting_status(db, slip),
        "source_refs": slip.source_refs,
    }
