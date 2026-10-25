"""A06 stage 2 endpoints: deterministic terms resolution.

`POST /terms/resolve` reuses `CAPABILITY_RUN_RECONCILIATION`, exactly as
A05's conflict-detection endpoint does and for the same reason: reading
verified sources, writing derived records and opening cases for humans is
the same family of work as a reconciliation run, not a human approval.
It spends no model credit -- it only reads claims A06 stage 1 already
extracted and code-verified. The nightly close reaches the same code
through its TERMS_RESOLVED milestone; this endpoint is the manual door
onto it, and the GET below is how a human (or lane B's scorer) inspects
what it decided.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_capability
from app.db import get_db
from app.models import CAPABILITY_CALCULATE_SETTLEMENT, CAPABILITY_RUN_RECONCILIATION, User
from app.models_ledger import VendorContractTerm
from app.terms_resolution import is_document_derived, resolve_festival_terms

router = APIRouter(prefix="/api/v1/terms", tags=["terms"])


class ResolveTermsRequest(BaseModel):
    festival_id: str
    business_date: date
    # Optional demo-clock override. Left out, the run resolves as of the
    # end of `business_date` in the business timezone, which is what makes
    # a re-run of the same night deterministic (DA19). Must carry a UTC
    # offset when supplied.
    as_of: datetime | None = None


class TermsCaseOpenedOut(BaseModel):
    case_id: str
    reason_code: str
    blocking: bool


class TermsVendorOutcomeOut(BaseModel):
    """What TERMS_RESOLVED decided for one vendor
    (app/terms_resolution.py `VendorTermsOutcome`).

    Every term value is `| None`, and null means "no signed document
    evidences this field" -- never zero. A screen that read null as 0%
    commission would invent a rate nobody agreed to."""

    vendor_id: str
    status: str  # RESOLVED | HELD
    action: str  # written | unchanged | no_signed_terms
    term_id: str | None
    term_version: int | None
    commission_bps: int | None
    minimum_fee_cents: int | None
    minimum_period: str | None
    processor_fee_deductible: bool | None
    conflicts_proposed: int
    # A LIST of case records. Note that `counts["cases_opened"]` next to it
    # is an int: the same name means the tally there and the records here.
    cases_opened: list[TermsCaseOpenedOut]
    case_ids: list[str]
    unresolved: list[str]
    # The resolver's full working: which claims it weighed, what it rejected
    # and why. Keys differ by `action`, so this stays an open object rather
    # than a model that would silently drop the unmodelled branches.
    detail: dict


class ResolveTermsOut(BaseModel):
    festival_id: str
    business_date: str
    as_of: str
    # Vendors per action and per status, plus `conflicts_proposed` and
    # `cases_opened`. The action/status keys are data, so this is an open map.
    counts: dict[str, int]
    vendors: list[TermsVendorOutcomeOut]


class VendorContractTermHistoryOut(BaseModel):
    """One recorded version of a vendor's terms. Old versions are never
    edited, so a list of these is the full history."""

    id: str
    version: int
    status: str
    # SIGNED_DOCUMENTS when documents established it, OTHER when a human did.
    basis: str
    commission_bps: int
    minimum_fee_cents: int | None
    processor_fee_deductible: bool
    effective_from: str | None
    # Null while this version is still the one in force.
    effective_to: str | None
    approved_by: str | None
    source_refs: list


@router.post("/resolve", status_code=201, response_model=ResolveTermsOut)
def resolve_terms(
    body: ResolveTermsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    if body.as_of is not None and body.as_of.tzinfo is None:
        raise HTTPException(status_code=422, detail="as_of must include a UTC offset")
    summary = resolve_festival_terms(
        db,
        festival_id=body.festival_id,
        business_date_=body.business_date,
        as_of=body.as_of,
        actor_id=user.id,
        actor_capability=CAPABILITY_RUN_RECONCILIATION,
    )
    db.commit()  # one commit point (C06)
    return {
        "festival_id": summary.festival_id,
        "business_date": summary.business_date.isoformat(),
        "as_of": summary.as_of.isoformat(),
        "counts": summary.counts(),
        "vendors": [
            {
                "vendor_id": v.vendor_id,
                "status": v.status,
                "action": v.action,
                "term_id": v.term_id,
                "term_version": v.term_version,
                "commission_bps": v.commission_bps,
                "minimum_fee_cents": v.minimum_fee_cents,
                "minimum_period": v.minimum_period,
                "processor_fee_deductible": v.processor_fee_deductible,
                "conflicts_proposed": v.conflicts_proposed,
                "cases_opened": v.cases_opened,
                "case_ids": v.case_ids,
                "unresolved": v.unresolved,
                "detail": v.detail,
            }
            for v in summary.vendors
        ],
    }


@router.get("/vendors/{vendor_id}", response_model=list[VendorContractTermHistoryOut])
def list_vendor_terms(
    vendor_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> list[dict]:
    """Every version ever recorded for this vendor, oldest first. Old
    versions are never edited, so this is the full history: which rate was
    in force when, what established it, and whether it came from signed
    documents or from a human decision."""
    rows = db.execute(
        select(VendorContractTerm)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version)
    ).scalars()
    return [
        {
            "id": t.id,
            "version": t.version,
            "status": t.status,
            "basis": "SIGNED_DOCUMENTS" if is_document_derived(t) else "OTHER",
            "commission_bps": t.commission_bps,
            "minimum_fee_cents": t.minimum_fee_cents,
            "processor_fee_deductible": t.processor_fee_deductible,
            "effective_from": t.effective_from.isoformat() if t.effective_from else None,
            "effective_to": t.effective_to.isoformat() if t.effective_to else None,
            "approved_by": t.approved_by,
            "source_refs": t.source_refs,
        }
        for t in rows
    ]
