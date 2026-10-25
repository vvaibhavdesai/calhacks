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

import hashlib
import json
from datetime import date, datetime

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
    CONTRACT_CONFLICT_REASON_CODE,
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
    # The row's optimistic-concurrency version. A reviewer resolves the case
    # they actually read, so the UI needs this to send `expected_version`.
    version: int
    run_id: str | None
    vendor_id: str | None
    business_date: date | None
    reason_code: str
    evidence_refs: list
    financial_impact_cents: int | None
    proposed_resolution: str | None
    required_capability: str | None
    status: str
    # Whether an OPEN case withholds the vendor's money. True for every
    # case kind before A06 stage 2; false only for `minimum_period_claim`,
    # where the signed contract already decides the night.
    blocking: bool
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
    # When the case was raised, last touched, and resolved. On the model all
    # along; simply never serialized, so no screen could order or age a case.
    opened_at: datetime | None
    updated_at: datetime | None
    resolved_at: datetime | None
    # The rates a reviewer may pick, derived from this case's evidence. Empty
    # for every kind but `vendor_contract_term_conflict` (see
    # `_resolution_options`), which keeps the generic approve/edit/reject.
    resolution_options: list[ResolutionOptionOut]
    # Digest of `evidence_refs`, so a decision can be bound to the evidence the
    # reviewer saw: if the evidence changes, the hash changes and a stale
    # approval is detectable. Deterministic over the refs, nothing else.
    evidence_hash: str | None


class ResolutionOptionOut(BaseModel):
    """One rate a human may choose when resolving a contract-term conflict.

    Derived from the case's own `evidence_refs`, never supplied by the caller:
    the options are exactly the candidate terms the detector cited, so a client
    cannot introduce a rate nobody detected.
    """

    option_id: str
    label: str
    # One line stating what choosing this does, so the reviewer is not left to
    # infer the consequence from a rate.
    effect_summary: str
    # What this choice is worth against the rate currently in force, from the
    # server's own figures. None when the case states no impact.
    impact_cents: int | None = None
    # True for the rate the case itself proposed, which is the one the existing
    # "approve" decision means; any other choice is an "edit".
    is_proposed: bool = False
    role: str | None = None
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    term_id: str | None = None


# Evidence roles the A05/A06 detectors attach to candidate terms, mapped to
# wording a reviewer can act on. An unknown role still yields an option, just
# labelled by its raw role, so a new detector never silently drops a candidate.
_ROLE_LABELS = {
    "contract_rate": "Signed contract",
    "producer_email_rate": "Producer email",
    "organizer_staff_rate": "Organizer staff claim",
    "amendment_rate": "Signed amendment",
    "vendor_claim_rate": "Vendor claim",
}


def _resolution_options(case: Case) -> list[ResolutionOptionOut]:
    """The choices for this case, or empty when the kind has no enumerable ones.

    Only `vendor_contract_term_conflict` has real options today: its evidence
    cites the competing candidate terms, each carrying its own rate. Every other
    reason code keeps the generic approve/edit/reject decision, and returns []
    so a client can tell "no options" from "options not modelled yet".
    """
    if case.reason_code != CONTRACT_CONFLICT_REASON_CODE:
        return []
    predicates = case.predicate_values or {}
    proposed_bps = predicates.get("proposed_bps")
    contract_bps = predicates.get("contract_bps")
    impact = case.financial_impact_cents
    options: list[ResolutionOptionOut] = []
    seen: set[str] = set()
    for ref in case.evidence_refs or []:
        if not isinstance(ref, dict):
            continue
        bps = ref.get("commission_bps")
        if not isinstance(bps, int):
            continue
        role = ref.get("role")
        # The term row id is unique per candidate; the role is the readable
        # fallback when a detector cited a rate without one.
        option_id = str(ref.get("ref_id") or role or f"option_{len(options) + 1}")
        if option_id in seen:
            continue
        seen.add(option_id)
        source = _ROLE_LABELS.get(role or "", (role or "Candidate").replace("_", " ").capitalize())
        # The case states one impact figure: the difference between the two
        # candidate rates. It applies to whichever option moves off the rate
        # currently in force (the signed contract), and is zero for keeping it.
        option_impact: int | None = None
        if isinstance(impact, int) and isinstance(contract_bps, int):
            option_impact = 0 if bps == contract_bps else impact
        options.append(
            ResolutionOptionOut(
                option_id=option_id,
                label=f"{source} — {bps / 100:.2f}%",
                effect_summary=(
                    f"Charge this vendor {bps / 100:.2f}% for this night only. "
                    "Approving it as a reusable policy is a separate decision."
                ),
                impact_cents=option_impact,
                is_proposed=bps == proposed_bps,
                role=role,
                commission_bps=bps,
                minimum_fee_cents=ref.get("minimum_fee_cents"),
                term_id=ref.get("ref_id"),
            )
        )
    return options


def _evidence_hash(evidence_refs: list) -> str | None:
    """Stable digest of a case's evidence references.

    Canonical JSON (sorted keys, no incidental whitespace) so the same refs
    always hash the same regardless of column or dict ordering. None when there
    is no evidence, rather than the hash of an empty list, so "no evidence" and
    "evidence that happens to be empty" stay distinguishable.
    """
    if not evidence_refs:
        return None
    canonical = json.dumps(evidence_refs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_case_out(case: Case) -> CaseOut:
    return CaseOut(
        id=case.id,
        version=case.version,
        run_id=case.run_id,
        vendor_id=case.vendor_id,
        business_date=case.business_date,
        reason_code=case.reason_code,
        evidence_refs=case.evidence_refs,
        financial_impact_cents=case.financial_impact_cents,
        proposed_resolution=case.proposed_resolution,
        required_capability=case.required_capability,
        status=case.status,
        blocking=case.blocking,
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
        opened_at=case.created_at,
        updated_at=case.updated_at,
        resolved_at=case.resolved_at,
        resolution_options=_resolution_options(case),
        evidence_hash=_evidence_hash(case.evidence_refs),
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
    # Same value under the name the reader's contract uses: the case whose
    # human decision this rule was approved from. It is the rule's provenance,
    # and the only place reuse can be approved, since a policy does not exist
    # before approval.
    origin_case_id: str | None


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
        origin_case_id=p.source_case_id,
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
    """Two accepted shapes for one command.

    The original shape states the rate directly: `decision` of
    approve/edit/reject plus `commission_bps`. Scripts and the verify suites
    use it and it is unchanged.

    The reviewer-facing shape names the option instead:
    `decision: "ACCEPT_OPTION" | "REJECT"` with `option_id`, and binds the
    decision to what the reviewer actually saw via `evidence_hash` and
    `expected_version`. The rate then comes from the case's own evidence, so
    the client never supplies the number that decides money.
    """

    decision: str  # approve | edit | reject | ACCEPT_OPTION | REJECT
    resolution_note: str | None = None
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    effective_to: date | None = None
    # Reviewer-facing envelope (C03).
    action: str | None = None
    option_id: str | None = None
    note: str | None = None
    # Safety bindings. Supplied by the reviewer-facing client; optional so the
    # original shape keeps working, enforced whenever present.
    evidence_hash: str | None = None
    expected_version: int | None = None


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

    # Bindings first: a decision made against a stale view of the case must
    # fail rather than apply to data the reviewer never saw (C03/C06).
    if body.expected_version is not None and body.expected_version != case.version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "the case changed since it was read",
                "expected_version": body.expected_version,
                "current_version": case.version,
            },
        )
    if body.evidence_hash is not None:
        current = _evidence_hash(case.evidence_refs)
        if body.evidence_hash != current:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "evidence_changed",
                    "message": "the case's evidence changed since it was read",
                    "expected_evidence_hash": body.evidence_hash,
                    "current_evidence_hash": current,
                },
            )

    decision = body.decision
    commission_bps = body.commission_bps
    minimum_fee_cents = body.minimum_fee_cents
    if decision in ("ACCEPT_OPTION", "REJECT"):
        if decision == "REJECT":
            decision = "reject"
        else:
            options = {o.option_id: o for o in _resolution_options(case)}
            if not options:
                raise HTTPException(
                    status_code=422,
                    detail=f"case {case.id} ({case.reason_code}) has no selectable options; "
                    "resolve it with decision approve/edit/reject and an explicit rate",
                )
            chosen = options.get(body.option_id or "")
            if chosen is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "unknown_option",
                        "message": "option_id is not one of this case's options",
                        "option_id": body.option_id,
                        "available": sorted(options),
                    },
                )
            # The rate comes from the case's evidence, never from the request.
            commission_bps = chosen.commission_bps
            minimum_fee_cents = (
                chosen.minimum_fee_cents
                if body.minimum_fee_cents is None
                else body.minimum_fee_cents
            )
            # `approve` means the rate the case proposed; anything else the
            # reviewer picks is an `edit`. Both are an explicit human decision.
            decision = "approve" if chosen.is_proposed else "edit"

    uow = UnitOfWork(db)
    try:
        case = resolve_case(
            db,
            uow,
            case=case,
            decision=decision,
            resolution_note=body.resolution_note or body.note,
            commission_bps=commission_bps,
            minimum_fee_cents=minimum_fee_cents,
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
    """Approve REUSE of the decision this case already made.

    Separate from resolving the case, deliberately: a one-time resolution is
    never auto-promoted into a reusable rule. The scope is the case's own
    vendor and effect; only the validity window is chosen here.
    """

    valid_from: date
    valid_to: date | None = None
    # Reviewer-facing envelope, matching POST /cases/{id}/resolve. Optional so
    # existing callers are unaffected; enforced whenever stated.
    action: str | None = None
    note: str | None = None
    expected_version: int | None = None
    evidence_hash: str | None = None


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
    # Same bindings as resolving the case: making a decision REUSABLE is the
    # larger of the two commitments, so it must not be made against a stale
    # view either.
    if body.expected_version is not None and body.expected_version != case.version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "the case changed since it was read",
                "expected_version": body.expected_version,
                "current_version": case.version,
            },
        )
    if body.evidence_hash is not None:
        current = _evidence_hash(case.evidence_refs)
        if body.evidence_hash != current:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "evidence_changed",
                    "message": "the case's evidence changed since it was read",
                    "expected_evidence_hash": body.evidence_hash,
                    "current_evidence_hash": current,
                },
            )
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
