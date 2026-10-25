"""A04 settlement endpoints.

Reuses `require_capability` from A01 (app.auth) exactly as A02's imports
router and A03's reconciliation router do -- no parallel authorization
mechanism. One write endpoint (trigger a calculation for one business_date,
every vendor in the festival); the rest are read endpoints exposing
settlements and their line items, including corrections.

`is_final_period` is an explicit required field on the calculate request,
not derived automatically from any festival calendar config -- A04 does
not own festival scheduling (that is C05/A06 territory). The caller (a
human operator today, a scheduler in A06 later) is responsible for marking
the correct business_date as the festival's last day. This is documented
plainly here and in docs/progress/A04-handoff.md as a conscious scope
boundary, not an oversight.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_capability
from app.db import get_db
from app.models import CAPABILITY_CALCULATE_SETTLEMENT, User
from app.models_ledger import Vendor
from app.models_settlement import Settlement, SettlementLineItem
from app.settlement import calculate_festival_settlements

router = APIRouter(prefix="/api/v1/settlements", tags=["settlements"])


class CalculateRequest(BaseModel):
    festival_id: str
    business_date: date
    is_final_period: bool = False
    run_id: str | None = None


# How each line bears on the figure, for presentation only -- the money is
# already decided by the time a line exists. FEE_BASIS is what a fee is
# computed on, FEE is a charge, DEDUCTION is money taken from funds in hand,
# PAYOUT is money owed out, PROTECTED is custody that is never a fee basis,
# MEMO is informational. An unmapped line type is shown as MEMO rather than
# guessed at.
_LINE_PRESENTATION = {
    "eligible_sales": ("Eligible sales", "FEE_BASIS"),
    "centrally_held": ("Centrally held", "MEMO"),
    "vendor_retained_cash": ("Vendor-retained cash", "MEMO"),
    "tax": ("Sales tax", "PROTECTED"),
    "tips": ("Tips", "PROTECTED"),
    "commission_assessed": ("Commission assessed", "FEE"),
    "fee_assessed": ("Fee assessed", "FEE"),
    "fee_collected_from_central": ("Fee collected from centrally-held funds", "DEDUCTION"),
    "processor_fee_assessed": ("Processor fee assessed", "FEE"),
    "processor_fee_collected": ("Processor fee collected", "DEDUCTION"),
    "vendor_fee_receivable": ("Fee receivable from vendor", "MEMO"),
    "unpaid_balance": ("Unpaid balance carried forward", "MEMO"),
    "vendor_payout": ("Vendor payout", "PAYOUT"),
    "additional_payout": ("Additional payout (correction)", "PAYOUT"),
    "overpayment_recovery": ("Overpayment recovery", "DEDUCTION"),
}


class SettlementLineItemOut(BaseModel):
    id: str
    line_type: str
    amount_cents: int
    source_refs: list
    # Same line under the names the reader's contract uses. Both are served so
    # the existing /line-items consumers keep working unchanged.
    line_id: str
    kind: str
    label: str
    treatment: str


class SettlementOut(BaseModel):
    id: str
    # Optimistic-concurrency version, and the hash of the input manifest this
    # calculation read. Both were on the row and never served, so no screen
    # could say which inputs produced the number.
    version: int
    source_manifest_hash: str | None
    festival_id: str
    vendor_id: str
    business_date: date
    calculation_version: str
    vendor_contract_term_id: str | None
    vendor_contract_term_version: int | None
    commission_bps_used: int
    minimum_fee_cents_used: int
    processor_fee_deductible_used: bool
    is_final_period: bool
    eligible_sales_cents: int
    centrally_held_cents: int
    vendor_retained_cash_cents: int
    tax_cents: int
    tips_cents: int
    cumulative_eligible_sales_cents: int
    cumulative_commission_assessed_prior_cents: int
    assessed_fee_cents: int
    collected_fee_cents: int
    vendor_fee_receivable_cents: int
    unpaid_balance_cents: int
    processor_fee_assessed_cents: int
    processor_fee_collected_cents: int
    payout_cents: int
    minimum_applied: bool
    holds: list
    correction_of: str | None
    correction_reason: str | None
    status: str
    # Policy version ids applied to this settlement (C03 serves ids; the full
    # records, with policy id and version, follow in `applied_policies` so the
    # trace keeps them).
    policy_refs: list[str]
    applied_policies: list[dict]
    # The lines the figure is made of. Also available on
    # GET /settlements/{id}/line-items; served here so one read explains the
    # number rather than two.
    line_items: list[SettlementLineItemOut]


class CalculateResultOut(BaseModel):
    settlement: SettlementOut
    is_correction: bool
    corrected_settlement_id: str | None
    additional_payout_cents: int | None = None
    overpayment_recovery_cents: int | None = None


class CalculateResponse(BaseModel):
    festival_id: str
    business_date: date
    results: list[CalculateResultOut]


def _line_item_out(li: SettlementLineItem) -> SettlementLineItemOut:
    label, treatment = _LINE_PRESENTATION.get(
        li.line_type, (li.line_type.replace("_", " ").capitalize(), "MEMO")
    )
    return SettlementLineItemOut(
        id=li.id,
        line_id=li.id,
        line_type=li.line_type,
        kind=li.line_type,
        label=label,
        treatment=treatment,
        amount_cents=li.amount_cents,
        source_refs=li.source_refs,
    )


def _policy_version_ids(policy_refs: list | None) -> list[str]:
    """The policy VERSION ids a settlement applied, in recorded order.

    `policy_refs` is stored as records ({policy_id, policy_version_id,
    version}); C03 serves the version ids, which is what identifies the exact
    rule that was applied. A record without one is skipped rather than guessed
    at from the policy id, which names the family and not the version.
    """
    ids: list[str] = []
    for ref in policy_refs or []:
        if isinstance(ref, dict) and isinstance(ref.get("policy_version_id"), str):
            ids.append(ref["policy_version_id"])
    return ids


def _to_settlement_out(s: Settlement) -> SettlementOut:
    return SettlementOut(
        id=s.id,
        festival_id=s.festival_id,
        vendor_id=s.vendor_id,
        business_date=s.business_date,
        calculation_version=s.calculation_version,
        vendor_contract_term_id=s.vendor_contract_term_id,
        vendor_contract_term_version=s.vendor_contract_term_version,
        commission_bps_used=s.commission_bps_used,
        minimum_fee_cents_used=s.minimum_fee_cents_used,
        processor_fee_deductible_used=s.processor_fee_deductible_used,
        is_final_period=s.is_final_period,
        eligible_sales_cents=s.eligible_sales_cents,
        centrally_held_cents=s.centrally_held_cents,
        vendor_retained_cash_cents=s.vendor_retained_cash_cents,
        tax_cents=s.tax_cents,
        tips_cents=s.tips_cents,
        cumulative_eligible_sales_cents=s.cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=s.cumulative_commission_assessed_prior_cents,
        assessed_fee_cents=s.assessed_fee_cents,
        collected_fee_cents=s.collected_fee_cents,
        vendor_fee_receivable_cents=s.vendor_fee_receivable_cents,
        unpaid_balance_cents=s.unpaid_balance_cents,
        processor_fee_assessed_cents=s.processor_fee_assessed_cents,
        processor_fee_collected_cents=s.processor_fee_collected_cents,
        payout_cents=s.payout_cents,
        minimum_applied=s.minimum_applied,
        holds=s.holds,
        correction_of=s.correction_of,
        correction_reason=s.correction_reason,
        status=s.status,
        version=s.version,
        source_manifest_hash=s.source_manifest_hash,
        policy_refs=_policy_version_ids(s.policy_refs),
        applied_policies=[r for r in (s.policy_refs or []) if isinstance(r, dict)],
        line_items=[
            _line_item_out(li)
            for li in sorted(s.line_items or [], key=lambda li: (li.line_type, li.id))
        ],
    )


@router.post("/calculate", response_model=CalculateResponse, status_code=201)
def calculate_settlements(
    body: CalculateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> CalculateResponse:
    outcomes = calculate_festival_settlements(
        db,
        festival_id=body.festival_id,
        business_date_=body.business_date,
        is_final_period=body.is_final_period,
        run_id=body.run_id,
        actor_id=user.id,
        actor_capability=CAPABILITY_CALCULATE_SETTLEMENT,
    )
    # Exactly one commit point (C06): every Settlement/SettlementLineItem
    # and every business_event appended during calculate_festival_settlements
    # lands together, or none do.
    db.commit()

    return CalculateResponse(
        festival_id=body.festival_id,
        business_date=body.business_date,
        results=[
            CalculateResultOut(
                settlement=_to_settlement_out(o.settlement),
                is_correction=o.is_correction,
                corrected_settlement_id=o.corrected_settlement_id,
                additional_payout_cents=(
                    o.adjustment.additional_payout_cents if o.adjustment else None
                ),
                overpayment_recovery_cents=(
                    o.adjustment.overpayment_recovery_cents if o.adjustment else None
                ),
            )
            for o in outcomes
        ],
    )


@router.get("", response_model=list[SettlementOut])
def list_settlements(
    festival_id: str | None = None,
    vendor_id: str | None = None,
    business_date: date | None = None,
    status_: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> list[SettlementOut]:
    query = select(Settlement)
    if festival_id is not None:
        query = query.where(Settlement.festival_id == festival_id)
    if vendor_id is not None:
        query = query.where(Settlement.vendor_id == vendor_id)
    if business_date is not None:
        query = query.where(Settlement.business_date == business_date)
    if status_ is not None:
        query = query.where(Settlement.status == status_)
    rows = db.execute(query.order_by(Settlement.created_at.asc())).scalars().all()
    return [_to_settlement_out(r) for r in rows]


@router.get("/vendors/{vendor_id}", response_model=list[SettlementOut])
def list_vendor_settlements(
    vendor_id: str,
    festival_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> list[SettlementOut]:
    # Registered BEFORE "/{settlement_id}" below: FastAPI matches routes in
    # registration order, and "vendors" would otherwise be swallowed as a
    # settlement_id path parameter.
    if db.get(Vendor, vendor_id) is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    query = select(Settlement).where(Settlement.vendor_id == vendor_id)
    if festival_id is not None:
        query = query.where(Settlement.festival_id == festival_id)
    rows = db.execute(query.order_by(Settlement.business_date.asc())).scalars().all()
    return [_to_settlement_out(r) for r in rows]


@router.get("/{settlement_id}", response_model=SettlementOut)
def get_settlement(
    settlement_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> SettlementOut:
    s = db.get(Settlement, settlement_id)
    if s is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    return _to_settlement_out(s)


@router.get("/{settlement_id}/line-items", response_model=list[SettlementLineItemOut])
def get_settlement_line_items(
    settlement_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> list[SettlementLineItemOut]:
    s = db.get(Settlement, settlement_id)
    if s is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    rows = (
        db.execute(
            select(SettlementLineItem).where(SettlementLineItem.settlement_id == settlement_id)
        )
        .scalars()
        .all()
    )
    return [_line_item_out(r) for r in rows]


@router.get("/{settlement_id}/corrections", response_model=list[SettlementOut])
def get_settlement_corrections(
    settlement_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CALCULATE_SETTLEMENT)),
) -> list[SettlementOut]:
    """Corrections linked to this settlement, discovered by querying
    `correction_of` -- never by following a mutated pointer on the
    original row (there isn't one; the original is never mutated)."""
    s = db.get(Settlement, settlement_id)
    if s is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    rows = (
        db.execute(
            select(Settlement)
            .where(Settlement.correction_of == settlement_id)
            .order_by(Settlement.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [_to_settlement_out(r) for r in rows]
