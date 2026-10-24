"""A07 controlled-payout endpoints: creation, approval, release, dispatch,
confirmation, cancellation and reconciliation, plus the minimal cash-pool
and vendor-beneficiary endpoints this task needs to exercise its own
re-checks over real HTTP.

Reuses `require_capability` unchanged from A01 (app.auth), same as every
other write endpoint in this codebase -- no parallel authorization
mechanism. `CAPABILITY_APPROVE_PAYOUT`/`CAPABILITY_FINANCE_LEAD` already
existed (seeded in A01's `ALL_CAPABILITY_CODES`); no new capability was
added for this task.

Seam for A06 (explicitly deferred per the task brief): every endpoint here
is a plain authenticated HTTP request gated by `require_capability`,
nothing assumes a human browser is the caller. `/payouts/{id}/dispatch` in
particular stands in for "the worker" in this task (A06's eventual
scheduler/LangGraph node, or any future background process, can call the
exact same endpoint or the underlying `app.payout_dispatch.dispatch_payout`
function without any change here) -- see docs/progress/A07-handoff.md.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.payout_dispatch as payout_dispatch
from app.auth import get_user_capability_codes, require_capability
from app.db import get_db
from app.models import CAPABILITY_APPROVE_PAYOUT, CAPABILITY_FINANCE_LEAD, User
from app.models_ledger import Payout
from app.models_payout_dispatch import CashPool, PayoutApproval, VendorDayPayoutAggregate

router = APIRouter(prefix="/api/v1/payouts", tags=["payouts"])
cash_pool_router = APIRouter(prefix="/api/v1/cash-pool", tags=["payouts"])
vendors_router = APIRouter(prefix="/api/v1/vendors", tags=["payouts"])


def _http_error_for(exc: Exception) -> HTTPException:
    if isinstance(exc, payout_dispatch.PayoutNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, payout_dispatch.PayoutVersionConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "expected": exc.expected, "current": exc.current},
        )
    if isinstance(exc, payout_dispatch.PayoutCapabilityError):
        return HTTPException(
            status_code=403, detail=f"missing required capability: {exc.required_capability}"
        )
    if isinstance(exc, payout_dispatch.PayoutBlockedError):
        return HTTPException(
            status_code=422,
            detail={"code": exc.reason_code, "details": exc.details},
        )
    return HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PayoutOut(BaseModel):
    id: str
    logical_obligation_id: str
    idempotency_key: str
    vendor_id: str
    business_date: date
    beneficiary_version: int
    amount_cents: int
    state: str
    provider_reference: str | None
    settlement_id: str | None
    bank_debit_confirmed_at: str | None = None
    payout_confirmed_at: str | None = None
    cancel_requested_at: str | None = None
    recovery_required: bool
    recovery_reason: str | None
    reservation_released_at: str | None = None
    dispatch_attempts: int
    version: int


def _to_out(p: Payout) -> PayoutOut:
    return PayoutOut(
        id=p.id,
        logical_obligation_id=p.logical_obligation_id,
        idempotency_key=p.idempotency_key,
        vendor_id=p.vendor_id,
        business_date=p.business_date,
        beneficiary_version=p.beneficiary_version,
        amount_cents=p.amount_cents,
        state=p.state,
        provider_reference=p.provider_reference,
        settlement_id=p.settlement_id,
        bank_debit_confirmed_at=(
            p.bank_debit_confirmed_at.isoformat() if p.bank_debit_confirmed_at else None
        ),
        payout_confirmed_at=p.payout_confirmed_at.isoformat() if p.payout_confirmed_at else None,
        cancel_requested_at=p.cancel_requested_at.isoformat() if p.cancel_requested_at else None,
        recovery_required=p.recovery_required,
        recovery_reason=p.recovery_reason,
        reservation_released_at=(
            p.reservation_released_at.isoformat() if p.reservation_released_at else None
        ),
        dispatch_attempts=p.dispatch_attempts,
        version=p.version,
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


class CreatePayoutRequest(BaseModel):
    settlement_id: str


class CreatePayoutResponse(BaseModel):
    payout: PayoutOut
    created: bool


@router.post("", response_model=CreatePayoutResponse, status_code=201)
def create_payout(
    body: CreatePayoutRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> CreatePayoutResponse:
    try:
        payout, created = payout_dispatch.create_payout_obligation(
            db,
            settlement_id=body.settlement_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
        )
    except payout_dispatch.PayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except payout_dispatch.PayoutBlockedError as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return CreatePayoutResponse(payout=_to_out(payout), created=created)


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    expected_version: int | None = None
    note: str | None = None


@router.post("/{payout_id}/approve", response_model=PayoutOut)
def approve(
    payout_id: str,
    body: ApproveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> PayoutOut:
    capability_codes = get_user_capability_codes(db, user.id)
    try:
        payout = payout_dispatch.approve_payout(
            db,
            payout_id=payout_id,
            actor_id=user.id,
            user_capability_codes=capability_codes,
            expected_version=body.expected_version,
            note=body.note,
        )
    except (
        payout_dispatch.PayoutNotFoundError,
        payout_dispatch.PayoutVersionConflictError,
        payout_dispatch.PayoutCapabilityError,
    ) as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return _to_out(payout)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class ReleaseRequest(BaseModel):
    expected_version: int | None = None


class ReleaseResponse(BaseModel):
    payout: PayoutOut
    changed: bool
    already_released: bool


@router.post("/{payout_id}/release", response_model=ReleaseResponse)
def release(
    payout_id: str,
    body: ReleaseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> ReleaseResponse:
    try:
        outcome = payout_dispatch.release_payout(
            db,
            payout_id=payout_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
            expected_version=body.expected_version,
        )
    except (
        payout_dispatch.PayoutNotFoundError,
        payout_dispatch.PayoutVersionConflictError,
        payout_dispatch.PayoutBlockedError,
    ) as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return ReleaseResponse(
        payout=_to_out(outcome.payout),
        changed=outcome.changed,
        already_released=outcome.already_released,
    )


# ---------------------------------------------------------------------------
# Dispatch (stands in for the worker/A06 scheduler -- see module docstring)
# ---------------------------------------------------------------------------


class DispatchRequest(BaseModel):
    simulate_outcome: str = "ACCEPTED"


class DispatchResponse(BaseModel):
    payout: PayoutOut
    outcome: str | None
    changed: bool
    newly_submitted: bool


@router.post("/{payout_id}/dispatch", response_model=DispatchResponse)
def dispatch(
    payout_id: str,
    body: DispatchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> DispatchResponse:
    try:
        outcome = payout_dispatch.dispatch_payout(
            db,
            payout_id=payout_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
            simulate_outcome=body.simulate_outcome,  # type: ignore[arg-type]
        )
    except payout_dispatch.PayoutNotFoundError as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return DispatchResponse(
        payout=_to_out(outcome.payout),
        outcome=outcome.outcome,
        changed=outcome.changed,
        newly_submitted=outcome.newly_submitted,
    )


# ---------------------------------------------------------------------------
# Confirmations (separate, idempotent events)
# ---------------------------------------------------------------------------


@router.post("/{payout_id}/confirm-bank-debit", response_model=PayoutOut)
def confirm_bank_debit(
    payout_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> PayoutOut:
    try:
        payout, _newly = payout_dispatch.confirm_bank_debit(
            db, payout_id=payout_id, actor_id=user.id, actor_capability=CAPABILITY_APPROVE_PAYOUT
        )
    except payout_dispatch.PayoutNotFoundError as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return _to_out(payout)


@router.post("/{payout_id}/confirm-payout", response_model=PayoutOut)
def confirm_payout(
    payout_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> PayoutOut:
    try:
        payout, _newly = payout_dispatch.confirm_payout_rail(
            db, payout_id=payout_id, actor_id=user.id, actor_capability=CAPABILITY_APPROVE_PAYOUT
        )
    except payout_dispatch.PayoutNotFoundError as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return _to_out(payout)


# ---------------------------------------------------------------------------
# Reconciliation (the only path out of UNKNOWN)
# ---------------------------------------------------------------------------


class ReconcileRequest(BaseModel):
    resolved_outcome: str


@router.post("/{payout_id}/reconcile", response_model=PayoutOut)
def reconcile(
    payout_id: str,
    body: ReconcileRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> PayoutOut:
    try:
        payout = payout_dispatch.reconcile_unknown_payout(
            db,
            payout_id=payout_id,
            resolved_outcome=body.resolved_outcome,  # type: ignore[arg-type]
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
        )
    except (payout_dispatch.PayoutNotFoundError, payout_dispatch.PayoutBlockedError) as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return _to_out(payout)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class CancelRequest(BaseModel):
    expected_version: int | None = None


class CancelResponse(BaseModel):
    payout: PayoutOut
    result: str


@router.post("/{payout_id}/cancel", response_model=CancelResponse)
def cancel(
    payout_id: str,
    body: CancelRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> CancelResponse:
    try:
        outcome = payout_dispatch.cancel_payout(
            db,
            payout_id=payout_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
            expected_version=body.expected_version,
        )
    except (
        payout_dispatch.PayoutNotFoundError,
        payout_dispatch.PayoutVersionConflictError,
    ) as exc:
        raise _http_error_for(exc) from exc
    db.commit()
    return CancelResponse(payout=_to_out(outcome.payout), result=outcome.result)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PayoutOut])
def list_payouts(
    vendor_id: str | None = None,
    business_date: date | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> list[PayoutOut]:
    query = select(Payout)
    if vendor_id is not None:
        query = query.where(Payout.vendor_id == vendor_id)
    if business_date is not None:
        query = query.where(Payout.business_date == business_date)
    if state is not None:
        query = query.where(Payout.state == state)
    rows = db.execute(query.order_by(Payout.created_at.asc())).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{payout_id}", response_model=PayoutOut)
def get_payout(
    payout_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> PayoutOut:
    p = db.get(Payout, payout_id)
    if p is None:
        raise HTTPException(status_code=404, detail="payout not found")
    return _to_out(p)


class PayoutApprovalOut(BaseModel):
    id: str
    actor_id: str | None
    capability: str
    decision: str
    amount_cents: int
    note: str | None


@router.get("/{payout_id}/approvals", response_model=list[PayoutApprovalOut])
def get_payout_approvals(
    payout_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> list[PayoutApprovalOut]:
    rows = (
        db.execute(
            select(PayoutApproval)
            .where(PayoutApproval.payout_id == payout_id)
            .order_by(PayoutApproval.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        PayoutApprovalOut(
            id=r.id,
            actor_id=r.actor_id,
            capability=r.capability,
            decision=r.decision,
            amount_cents=r.amount_cents,
            note=r.note,
        )
        for r in rows
    ]


class VendorDayAggregateOut(BaseModel):
    vendor_id: str
    business_date: date
    committed_cents: int


@vendors_router.get(
    "/{vendor_id}/payout-aggregate",
    response_model=VendorDayAggregateOut,
    tags=["payouts"],
)
def get_vendor_day_aggregate(
    vendor_id: str,
    business_date: date,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> VendorDayAggregateOut:
    row = db.execute(
        select(VendorDayPayoutAggregate).where(
            VendorDayPayoutAggregate.vendor_id == vendor_id,
            VendorDayPayoutAggregate.business_date == business_date,
        )
    ).scalar_one_or_none()
    return VendorDayAggregateOut(
        vendor_id=vendor_id,
        business_date=business_date,
        committed_cents=0 if row is None else row.committed_cents,
    )


# ---------------------------------------------------------------------------
# Cash pool (minimal placeholder -- see app/payout_dispatch.py module note)
# ---------------------------------------------------------------------------


class FundCashPoolRequest(BaseModel):
    festival_id: str
    additional_cents: int


class CashPoolOut(BaseModel):
    festival_id: str
    available_cents: int
    reserved_cents: int
    version: int


@cash_pool_router.post("/fund", response_model=CashPoolOut)
def fund_cash_pool(
    body: FundCashPoolRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_FINANCE_LEAD)),
) -> CashPoolOut:
    pool = payout_dispatch.fund_cash_pool(
        db,
        festival_id=body.festival_id,
        additional_cents=body.additional_cents,
        actor_id=user.id,
        actor_capability=CAPABILITY_FINANCE_LEAD,
    )
    db.commit()
    return CashPoolOut(
        festival_id=pool.festival_id,
        available_cents=pool.available_cents,
        reserved_cents=pool.reserved_cents,
        version=pool.version,
    )


@cash_pool_router.get("/{festival_id}", response_model=CashPoolOut)
def get_cash_pool(
    festival_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_PAYOUT)),
) -> CashPoolOut:
    pool = db.execute(
        select(CashPool).where(CashPool.festival_id == festival_id)
    ).scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail="cash pool not found for festival")
    return CashPoolOut(
        festival_id=pool.festival_id,
        available_cents=pool.available_cents,
        reserved_cents=pool.reserved_cents,
        version=pool.version,
    )


# ---------------------------------------------------------------------------
# Vendor beneficiary (minimal hook -- see app/payout_dispatch.py module note)
# ---------------------------------------------------------------------------


class UpdateBeneficiaryRequest(BaseModel):
    beneficiary_details: dict


class VendorOut(BaseModel):
    id: str
    version: int
    beneficiary_details: dict


@vendors_router.patch("/{vendor_id}/beneficiary", response_model=VendorOut, tags=["payouts"])
def update_vendor_beneficiary(
    vendor_id: str,
    body: UpdateBeneficiaryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_FINANCE_LEAD)),
) -> VendorOut:
    try:
        vendor = payout_dispatch.update_vendor_beneficiary(
            db,
            vendor_id=vendor_id,
            beneficiary_details=body.beneficiary_details,
            actor_id=user.id,
            actor_capability=CAPABILITY_FINANCE_LEAD,
        )
    except payout_dispatch.PayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return VendorOut(
        id=vendor.id, version=vendor.version, beneficiary_details=vendor.beneficiary_details
    )
