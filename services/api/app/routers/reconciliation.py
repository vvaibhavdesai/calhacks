"""A03 reconciliation endpoints.

Reuses `require_capability` from A01 (app.auth) exactly as A02's imports
router does -- no parallel authorization mechanism. One write endpoint
(trigger a run); the rest are read endpoints exposing match groups and
cases, per the task instructions ("does not need new write/approval
endpoints beyond exposing match groups and cases").
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_capability
from app.db import get_db
from app.models import CAPABILITY_RUN_RECONCILIATION, User
from app.models_ledger import Case
from app.models_reconciliation import MatchGroup, MatchGroupMember
from app.reconciliation import reconcile_festival

router = APIRouter(prefix="/api/v1/reconciliation", tags=["reconciliation"])


class RunRequest(BaseModel):
    festival_id: str
    as_of: date | None = None


class VendorResultOut(BaseModel):
    vendor_id: str
    order_groups: int
    cash_groups: int
    cases_opened: int
    error: str | None = None


class RunResponse(BaseModel):
    run_id: str
    festival_id: str
    status: str
    vendor_results: list[VendorResultOut]
    bank_deposit_groups: int
    bank_deposit_cases: int
    total_cases_opened: int


@router.post("/runs", response_model=RunResponse, status_code=201)
def create_reconciliation_run(
    body: RunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> RunResponse:
    summary = reconcile_festival(
        db,
        festival_id=body.festival_id,
        as_of=body.as_of,
        actor_id=user.id,
        actor_capability=CAPABILITY_RUN_RECONCILIATION,
    )
    # Exactly one commit point: the Run, every MatchGroup/MatchGroupMember,
    # every Case, and every business_event appended during
    # reconcile_festival() all land together, or none do (C06).
    db.commit()

    return RunResponse(
        run_id=summary.run_id,
        festival_id=summary.festival_id,
        status=summary.status,
        vendor_results=[
            VendorResultOut(
                vendor_id=v.vendor_id,
                order_groups=v.order_groups,
                cash_groups=v.cash_groups,
                cases_opened=v.cases_opened,
                error=v.error,
            )
            for v in summary.vendor_results
        ],
        bank_deposit_groups=summary.bank_deposit_groups,
        bank_deposit_cases=summary.bank_deposit_cases,
        total_cases_opened=summary.total_cases_opened,
    )


class MatchGroupMemberOut(BaseModel):
    member_kind: str
    member_id: str
    role: str
    amount_cents: int


class MatchGroupOut(BaseModel):
    id: str
    run_id: str | None
    festival_id: str
    vendor_id: str | None
    match_type: str
    business_date: date | None
    matched_amount_cents: int
    variance_cents: int
    reason_code: str
    status: str
    case_id: str | None
    members: list[MatchGroupMemberOut]


@router.get("/groups", response_model=list[MatchGroupOut])
def list_match_groups(
    festival_id: str | None = None,
    vendor_id: str | None = None,
    run_id: str | None = None,
    match_type: str | None = None,
    status_: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> list[MatchGroupOut]:
    query = select(MatchGroup)
    if festival_id is not None:
        query = query.where(MatchGroup.festival_id == festival_id)
    if vendor_id is not None:
        query = query.where(MatchGroup.vendor_id == vendor_id)
    if run_id is not None:
        query = query.where(MatchGroup.run_id == run_id)
    if match_type is not None:
        query = query.where(MatchGroup.match_type == match_type)
    if status_ is not None:
        query = query.where(MatchGroup.status == status_)
    groups = db.execute(query.order_by(MatchGroup.created_at.asc())).scalars().all()
    return [_to_group_out(db, g) for g in groups]


@router.get("/groups/{group_id}", response_model=MatchGroupOut)
def get_match_group(
    group_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> MatchGroupOut:
    group = db.get(MatchGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="match group not found")
    return _to_group_out(db, group)


def _to_group_out(db: Session, group: MatchGroup) -> MatchGroupOut:
    members = (
        db.execute(select(MatchGroupMember).where(MatchGroupMember.match_group_id == group.id))
        .scalars()
        .all()
    )
    return MatchGroupOut(
        id=group.id,
        run_id=group.run_id,
        festival_id=group.festival_id,
        vendor_id=group.vendor_id,
        match_type=group.match_type,
        business_date=group.business_date,
        matched_amount_cents=group.matched_amount_cents,
        variance_cents=group.variance_cents,
        reason_code=group.reason_code,
        status=group.status,
        case_id=group.case_id,
        members=[
            MatchGroupMemberOut(
                member_kind=m.member_kind,
                member_id=m.member_id,
                role=m.role,
                amount_cents=m.amount_cents,
            )
            for m in members
        ],
    )


class CaseOut(BaseModel):
    id: str
    run_id: str | None
    vendor_id: str | None
    reason_code: str
    evidence_refs: list
    financial_impact_cents: int | None
    proposed_resolution: str | None
    required_capability: str | None
    status: str


@router.get("/cases", response_model=list[CaseOut])
def list_cases(
    vendor_id: str | None = None,
    run_id: str | None = None,
    status_: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> list[CaseOut]:
    query = select(Case)
    if vendor_id is not None:
        query = query.where(Case.vendor_id == vendor_id)
    if run_id is not None:
        query = query.where(Case.run_id == run_id)
    if status_ is not None:
        query = query.where(Case.status == status_)
    cases = db.execute(query.order_by(Case.created_at.asc())).scalars().all()
    return [_to_case_out(c) for c in cases]


@router.get("/cases/{case_id}", response_model=CaseOut)
def get_case(
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return _to_case_out(case)


def _to_case_out(case: Case) -> CaseOut:
    return CaseOut(
        id=case.id,
        run_id=case.run_id,
        vendor_id=case.vendor_id,
        reason_code=case.reason_code,
        evidence_refs=case.evidence_refs,
        financial_impact_cents=case.financial_impact_cents,
        proposed_resolution=case.proposed_resolution,
        required_capability=case.required_capability,
        status=case.status,
    )
