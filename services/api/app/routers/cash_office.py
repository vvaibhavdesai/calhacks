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


@router.post("/cash-office/counts", status_code=201)
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


@router.post("/cash-office/deposit-slips", status_code=201)
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


@router.get("/cash-office")
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


@router.post("/cash-office/reconcile")
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


@router.post("/bank-entries/scan")
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


@router.post("/chargeback-fees", status_code=201)
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


@router.get("/chargeback-fees")
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


@router.post("/cash-cases/{case_id}/decision")
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


@router.get("/cash-cases")
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


@router.get("/cash-office/deposit-slips/{slip_id}")
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
