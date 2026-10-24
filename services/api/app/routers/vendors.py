"""Vendor reads: the vendor list and one vendor's record.

Why this is separate from `app/routers/payouts.py`, which already owns a
`/api/v1/vendors` router: that module's two vendor routes
(`PATCH /{vendor_id}/beneficiary`, `GET /{vendor_id}/payout-aggregate`) are
payout-control surfaces gated on `approve_payout`/`finance_lead`, and A07's
module docstring is explicit that they are the minimal hook its re-checks
need rather than a vendor surface. These are plain reads for any
authenticated user, the same bar as A08's finance reads, so they live in
their own router and the two are mounted side by side in app/main.py. The
path prefixes overlap but no path does, because a path parameter never
matches across a `/`.

Not served here, deliberately: `beneficiary_details`. It is the vendor's
payout destination (bank/routing data in a real deployment), and every
existing route that returns it requires `finance_lead`. Turning a
`vendor_id` into a name must not become the cheap way to read banking
details, so this reports only whether a beneficiary has been configured and
at which version. `PATCH /vendors/{id}/beneficiary` remains the
`finance_lead`-gated route that returns the details themselves.

Contract terms are returned as the rows they are, newest version first.
Which term applies on a given business date is A04's deterministic
resolution (`app/settlement.py`, pinned onto every settlement row), not
something this read re-derives -- a second, subtly different resolution
living in a read endpoint is exactly the kind of divergence C01 warns
about.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.models_ledger import Vendor, VendorContractTerm

router = APIRouter(prefix="/api/v1/vendors", tags=["vendors"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VendorSummaryOut(BaseModel):
    id: str
    festival_id: str
    name: str
    display_name: str | None
    status: str
    version: int


class VendorListOut(BaseModel):
    items: list[VendorSummaryOut]
    next_cursor: str | None


class VendorContractTermOut(BaseModel):
    id: str
    version: int
    status: str
    # Null means "use the festival default" (15% / $500 per the brief), not
    # zero -- see app/models_ledger.py's VendorContractTerm.
    commission_bps: int | None
    minimum_fee_cents: int | None
    processor_fee_deductible: bool
    effective_from: date
    effective_to: date | None
    source_refs: list


class VendorDetailOut(BaseModel):
    id: str
    festival_id: str
    name: str
    display_name: str | None
    status: str
    version: int
    # `Vendor.version` is also the beneficiary version A07 freezes onto a
    # payout at approval and re-checks at release (Payout.beneficiary_version
    # in app/payout_dispatch.py). Reported under both names because a caller
    # comparing against a payout should not have to know they are the same
    # number.
    beneficiary_version: int
    beneficiary_configured: bool
    contract_terms: list[VendorContractTermOut]
    created_at: datetime
    updated_at: datetime


def _summary_out(v: Vendor) -> VendorSummaryOut:
    return VendorSummaryOut(
        id=v.id,
        festival_id=v.festival_id,
        name=v.name,
        display_name=v.display_name,
        status=v.status,
        version=v.version,
    )


def _term_out(t: VendorContractTerm) -> VendorContractTermOut:
    return VendorContractTermOut(
        id=t.id,
        version=t.version,
        status=t.status,
        commission_bps=t.commission_bps,
        minimum_fee_cents=t.minimum_fee_cents,
        processor_fee_deductible=t.processor_fee_deductible,
        effective_from=t.effective_from,
        effective_to=t.effective_to,
        source_refs=t.source_refs,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=VendorListOut)
def list_vendors(
    festival_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VendorListOut:
    """Cursor paging, same offset-cursor convention as `GET /journal`: the
    cursor is an opaque decimal offset over a stable (name, id) order, and
    `next_cursor` is null on the last page. `festival_id` is optional so a
    single vendor-name lookup table can be loaded without one."""
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    query = select(Vendor)
    if festival_id is not None:
        query = query.where(Vendor.festival_id == festival_id)
    if status is not None:
        query = query.where(Vendor.status == status)
    rows = (
        db.execute(
            query.order_by(Vendor.name.asc(), Vendor.id.asc()).offset(offset).limit(limit + 1)
        )
        .scalars()
        .all()
    )
    return VendorListOut(
        items=[_summary_out(v) for v in rows[:limit]],
        next_cursor=str(offset + limit) if len(rows) > limit else None,
    )


@router.get("/{vendor_id}", response_model=VendorDetailOut)
def get_vendor(
    vendor_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VendorDetailOut:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    terms = (
        db.execute(
            select(VendorContractTerm)
            .where(VendorContractTerm.vendor_id == vendor_id)
            .order_by(VendorContractTerm.version.desc())
        )
        .scalars()
        .all()
    )
    return VendorDetailOut(
        id=vendor.id,
        festival_id=vendor.festival_id,
        name=vendor.name,
        display_name=vendor.display_name,
        status=vendor.status,
        version=vendor.version,
        beneficiary_version=vendor.version,
        beneficiary_configured=bool(vendor.beneficiary_details),
        contract_terms=[_term_out(t) for t in terms],
        created_at=vendor.created_at,
        updated_at=vendor.updated_at,
    )
