"""A05 case/policy endpoints.

Reuses `require_capability` unchanged from A01, same as every prior task's
router. `CAPABILITY_RESOLVE_CASE` and `CAPABILITY_APPROVE_POLICY` already
exist (A01's `app.models.ALL_CAPABILITY_CODES`, already seeded onto the
admin user by `scripts/seed_admin.py`) -- this task does not define new
capability constants. The conflict-DETECTION endpoint reuses
`CAPABILITY_RUN_RECONCILIATION` (A03) rather than inventing a new
capability: detecting a source conflict and opening/auto-resolving a case
from it is the same family of work as A03's reconciliation runs (read
sources, classify, open a case when unresolved), not a human approval
action -- `resolve`/`approve-policy`/`challenge` below are the human-
approval endpoints, gated by `resolve_case`/`approve_policy` instead.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_capability
from app.db import get_db
from app.models import (
    CAPABILITY_APPROVE_POLICY,
    CAPABILITY_RESOLVE_CASE,
    CAPABILITY_RUN_RECONCILIATION,
    User,
)
from app.models_ledger import Case, PolicyVersion
from app.policy import (
    approve_case_policy,
    challenge_case,
    detect_contract_term_conflicts_for_festival,
    resolve_case,
    revise_policy,
    suspend_policy,
)
from app.uow import UnitOfWork

router = APIRouter(prefix="/api/v1/cases", tags=["cases"])
policies_router = APIRouter(prefix="/api/v1/policies", tags=["policies"])


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class CaseOut(BaseModel):
    id: str
    run_id: str | None
    vendor_id: str | None
    business_date: date | None
    reason_code: str
    evidence_refs: list
    financial_impact_cents: int | None
    proposed_resolution: str | None
    required_capability: str | None
    status: str
    predicate_values: dict
    resolution_decision: str | None
    resolution_note: str | None
    resolution_effect: dict | None
    resolved_by: str | None
    resolved_capability: str | None
    auto_resolved: bool
    applied_policy_version_id: str | None
    origin_case_id: str | None
    challenge_status: str | None
    subject_type: str | None
    subject_id: str | None


def _to_case_out(case: Case) -> CaseOut:
    return CaseOut(
        id=case.id,
        run_id=case.run_id,
        vendor_id=case.vendor_id,
        business_date=case.business_date,
        reason_code=case.reason_code,
        evidence_refs=case.evidence_refs,
        financial_impact_cents=case.financial_impact_cents,
        proposed_resolution=case.proposed_resolution,
        required_capability=case.required_capability,
        status=case.status,
        predicate_values=case.predicate_values,
        resolution_decision=case.resolution_decision,
        resolution_note=case.resolution_note,
        resolution_effect=case.resolution_effect,
        resolved_by=case.resolved_by,
        resolved_capability=case.resolved_capability,
        auto_resolved=case.auto_resolved,
        applied_policy_version_id=case.applied_policy_version_id,
        origin_case_id=case.origin_case_id,
        challenge_status=case.challenge_status,
        subject_type=case.subject_type,
        subject_id=case.subject_id,
    )


class PolicyOut(BaseModel):
    id: str
    policy_id: str
    version: int
    scope: dict
    predicates: dict
    effect: dict
    evidence_refs: list
    approver_ref: str | None
    valid_from: date | None
    valid_to: date | None
    status: str
    supersedes_version: int | None
    source_case_id: str | None


def _to_policy_out(p: PolicyVersion) -> PolicyOut:
    return PolicyOut(
        id=p.id,
        policy_id=p.policy_id,
        version=p.version,
        scope=p.scope,
        predicates=p.predicates,
        effect=p.effect,
        evidence_refs=p.evidence_refs,
        approver_ref=p.approver_ref,
        valid_from=p.valid_from,
        valid_to=p.valid_to,
        status=p.status,
        supersedes_version=p.supersedes_version,
        source_case_id=p.source_case_id,
    )


# ---------------------------------------------------------------------------
# Detection (opens OR auto-resolves cases)
# ---------------------------------------------------------------------------


class DetectConflictsRequest(BaseModel):
    festival_id: str
    business_date: date
    eligible_sales_cents_by_vendor: dict[str, int]
    run_id: str | None = None


class DetectConflictsResponse(BaseModel):
    festival_id: str
    business_date: date
    cases: list[CaseOut]


@router.post("/detect-contract-conflicts", response_model=DetectConflictsResponse, status_code=201)
def detect_contract_conflicts(
    body: DetectConflictsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> DetectConflictsResponse:
    cases = detect_contract_term_conflicts_for_festival(
        db,
        festival_id=body.festival_id,
        business_date_=body.business_date,
        eligible_sales_cents_by_vendor=body.eligible_sales_cents_by_vendor,
        run_id=body.run_id,
        actor_id=user.id,
        actor_capability=CAPABILITY_RUN_RECONCILIATION,
    )
    # Exactly one commit point (C06): every Case and business_event from
    # this detection pass lands together, or none do.
    db.commit()
    return DetectConflictsResponse(
        festival_id=body.festival_id,
        business_date=body.business_date,
        cases=[_to_case_out(c) for c in cases],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CaseOut])
def list_cases(
    vendor_id: str | None = None,
    status_: str | None = None,
    reason_code: str | None = None,
    auto_resolved: bool | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> list[CaseOut]:
    query = select(Case)
    if vendor_id is not None:
        query = query.where(Case.vendor_id == vendor_id)
    if status_ is not None:
        query = query.where(Case.status == status_)
    if reason_code is not None:
        query = query.where(Case.reason_code == reason_code)
    if auto_resolved is not None:
        query = query.where(Case.auto_resolved == auto_resolved)
    rows = db.execute(query.order_by(Case.created_at.asc())).scalars().all()
    return [_to_case_out(c) for c in rows]


@router.get("/{case_id}", response_model=CaseOut)
def get_case(
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return _to_case_out(case)


# ---------------------------------------------------------------------------
# Commands: resolve (one-time), approve-policy (reusable, SEPARATE), challenge
# ---------------------------------------------------------------------------


class ResolveCaseRequest(BaseModel):
    decision: str  # "approve" | "edit" | "reject"
    resolution_note: str | None = None
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    effective_to: date | None = None


@router.post("/{case_id}/resolve", response_model=CaseOut, status_code=200)
def resolve_case_endpoint(
    case_id: str,
    body: ResolveCaseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    uow = UnitOfWork(db)
    try:
        case = resolve_case(
            db,
            uow,
            case=case,
            decision=body.decision,
            resolution_note=body.resolution_note,
            commission_bps=body.commission_bps,
            minimum_fee_cents=body.minimum_fee_cents,
            effective_to=body.effective_to,
            actor_id=user.id,
            actor_capability=CAPABILITY_RESOLVE_CASE,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Exactly one commit point (C06).
    db.commit()
    return _to_case_out(case)


class ApprovePolicyRequest(BaseModel):
    valid_from: date
    valid_to: date | None = None


@router.post("/{case_id}/approve-policy", response_model=PolicyOut, status_code=201)
def approve_policy_endpoint(
    case_id: str,
    body: ApprovePolicyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> PolicyOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    uow = UnitOfWork(db)
    try:
        policy = approve_case_policy(
            db,
            uow,
            case=case,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_POLICY,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _to_policy_out(policy)


class ChallengeCaseRequest(BaseModel):
    note: str


@router.post("/{case_id}/challenge", response_model=CaseOut, status_code=200)
def challenge_case_endpoint(
    case_id: str,
    body: ChallengeCaseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> CaseOut:
    """DATA MODEL / ACTION HOOK ONLY -- see app/policy.py's challenge_case
    docstring. A09 builds the real preview/pause/correction workflow on
    top of the `challenge_status`/`challenged_by`/`challenged_at`/
    `challenge_note` columns this sets."""
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    uow = UnitOfWork(db)
    try:
        case = challenge_case(
            db,
            uow,
            case=case,
            note=body.note,
            actor_id=user.id,
            actor_capability=CAPABILITY_RESOLVE_CASE,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _to_case_out(case)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@policies_router.get("", response_model=list[PolicyOut])
def list_policies(
    policy_id: str | None = None,
    status_: str | None = None,
    vendor_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> list[PolicyOut]:
    query = select(PolicyVersion)
    if policy_id is not None:
        query = query.where(PolicyVersion.policy_id == policy_id)
    if status_ is not None:
        query = query.where(PolicyVersion.status == status_)
    rows = db.execute(query.order_by(PolicyVersion.created_at.asc())).scalars().all()
    if vendor_id is not None:
        rows = [p for p in rows if p.scope.get("vendor_id") == vendor_id]
    return [_to_policy_out(p) for p in rows]


@policies_router.get("/{policy_version_id}", response_model=PolicyOut)
def get_policy(
    policy_version_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> PolicyOut:
    p = db.get(PolicyVersion, policy_version_id)
    if p is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    return _to_policy_out(p)


@policies_router.get("/{policy_version_id}/applications", response_model=list[CaseOut])
def get_policy_applications(
    policy_version_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> list[CaseOut]:
    """Every case this EXACT policy version auto-resolved -- "old
    applications of a suspended policy remain visible/traceable", proven
    by this endpoint still returning the same rows after the policy's
    status later changes to SUSPENDED/SUPERSEDED (the query is by
    `applied_policy_version_id`, an immutable row reference, not by
    "whatever policy_id's current ACTIVE version is")."""
    p = db.get(PolicyVersion, policy_version_id)
    if p is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    rows = (
        db.execute(
            select(Case)
            .where(Case.applied_policy_version_id == policy_version_id)
            .order_by(Case.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [_to_case_out(c) for c in rows]


class SuspendPolicyRequest(BaseModel):
    reason: str


@policies_router.post("/{policy_version_id}/suspend", response_model=PolicyOut, status_code=200)
def suspend_policy_endpoint(
    policy_version_id: str,
    body: SuspendPolicyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> PolicyOut:
    p = db.get(PolicyVersion, policy_version_id)
    if p is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    uow = UnitOfWork(db)
    try:
        p = suspend_policy(
            db,
            uow,
            policy=p,
            reason=body.reason,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_POLICY,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _to_policy_out(p)


class RevisePolicyRequest(BaseModel):
    effect: dict
    valid_from: date
    valid_to: date | None = None
    evidence_refs: list | None = None


@policies_router.post("/{policy_version_id}/revise", response_model=PolicyOut, status_code=201)
def revise_policy_endpoint(
    policy_version_id: str,
    body: RevisePolicyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_POLICY)),
) -> PolicyOut:
    p = db.get(PolicyVersion, policy_version_id)
    if p is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    uow = UnitOfWork(db)
    try:
        new_p = revise_policy(
            db,
            uow,
            policy=p,
            new_effect=body.effect,
            new_valid_from=body.valid_from,
            new_valid_to=body.valid_to,
            evidence_refs=body.evidence_refs,
            actor_id=user.id,
            actor_capability=CAPABILITY_APPROVE_POLICY,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _to_policy_out(new_p)
