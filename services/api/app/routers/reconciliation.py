"""A03 reconciliation endpoints.

Reuses `require_capability` from A01 (app.auth) exactly as A02's imports
router does -- no parallel authorization mechanism. One write endpoint
(trigger a run); the rest are read endpoints exposing match groups and
cases, per the task instructions ("does not need new write/approval
endpoints beyond exposing match groups and cases").
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, tuple_
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
    # Same value under the name the reader's contract uses.
    match_kind: str
    # What this group was matched out of: one entry per member, so a reader can
    # drill from the group to the order, payment, bank entry or cash count it
    # was computed from.
    member_refs: list[MatchGroupMemberOut]
    member_kinds: list[str]
    # Totals per side, keyed `<kind>.<role>` (order.expected, payment.capture,
    # payment.refund, ...). This is what makes a variance legible: the two
    # sides are shown rather than only their difference.
    matched_amounts: dict[str, int]
    # The group's own provenance column, which was never served.
    source_refs: list
    # One sentence for the reason code, so a reader is not left decoding it.
    explanation: str


# A sentence per reason code. The classifier's codes are stable and few; an
# unmapped one is humanised from the code rather than guessed at, so a new
# classification shows up readably instead of blank.
_RECONCILIATION_EXPLANATIONS = {
    "order_payment_match": ("The payments captured for this order sum to the order total."),
    "cash_count_match": (
        "The counted cash matches what this vendor's orders say was taken in cash."
    ),
    "deposit_settled_no_fee": (
        "This bank deposit settled for the amount expected, with no fee deducted."
    ),
    "deposit_pending_settlement": ("The deposit is expected but has not settled at the bank yet."),
    "deposit_settlement_overdue": ("The deposit has not settled within the window allowed for it."),
    "processor_fee_explained": (
        "The shortfall against the gross is exactly the processor's stated fee."
    ),
    "refund_applied": ("A refund was recorded against this order and is netted out of its sales."),
    "chargeback_applied": (
        "A chargeback was recorded against this order and is netted out of its sales."
    ),
    "duplicate_charge_refunded": "The order was charged twice and the duplicate was refunded.",
    "suspected_duplicate_charge": (
        "Two captures of the same amount with no refund -- a human decides."
    ),
    "refund_exceeds_captured_amount": "More was refunded than was ever captured for this order.",
    "order_overcharged": ("More was captured than the order total, and no refund explains it."),
    "missing_or_partial_payment": "The order is not fully covered by captured payments.",
    "cash_variance": (
        "The counted cash does not match what the orders say, and no count explains it."
    ),
    "bank_deposit_variance_unexplained": (
        "The bank deposit does not match the expected amount, and no fee or refund explains it."
    ),
}


def _explanation(reason_code: str) -> str:
    stated = _RECONCILIATION_EXPLANATIONS.get(reason_code)
    return stated or reason_code.replace("_", " ").capitalize() + "."


# The status values this service actually writes (app/reconciliation.py) are
# "matched", "case" and "pending". Lane B's DRAFT document proposed a louder
# vocabulary ("MATCHED", "CASE_REQUIRED", "TIMING_PENDING", ...) and the web
# client is generated from that draft, so it filters with those spellings.
# Both are accepted here and mapped onto what is stored. This is the cheap
# half of a real divergence: the filter silently matched nothing before, which
# looked like "no exceptions exist" rather than "that filter does not work".
# Which spelling becomes canonical is a C03 contract decision, not this
# router's to make.
_GROUP_STATUSES = {
    "matched": "matched",
    "MATCHED": "matched",
    "case": "case",
    "CASE_REQUIRED": "case",
    "pending": "pending",
    "TIMING_PENDING": "pending",
}


@router.get("/groups", response_model=list[MatchGroupOut])
def list_match_groups(
    festival_id: str | None = None,
    vendor_id: str | None = None,
    run_id: str | None = None,
    match_type: str | None = None,
    business_date: date | None = None,
    status: str | None = None,
    status_: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> list[MatchGroupOut]:
    """Match groups, oldest first, one page at a time.

    This route previously took no `limit` and returned every matching row,
    each one costing its own members query. On the real festival that is
    201,035 groups: ~220 MB of JSON and ~32 seconds, which reads as a screen
    stuck on "loading" rather than as a slow response. A reconciliation
    database only grows, so an unbounded list here can never be safe.

    Paging is keyset, not OFFSET: `cursor` is the id of the last group on the
    previous page and the scan resumes after that row's (created_at, id). An
    OFFSET page would re-scan everything before it and would also skip or
    repeat rows when a new run inserts groups mid-read, which is exactly what
    happens here while a close is running.

    `status` accepts either spelling (see _GROUP_STATUSES) and refuses an
    unknown one rather than returning an unfiltered page, and
    `business_date` is honoured -- the screen sent both already.

    The response stays a bare array, as it was, so existing callers are
    unaffected; the client treats a bare array as one unpaged page. The next
    page is requested with `cursor=<id of the last item>`.
    """
    query = select(MatchGroup)
    if festival_id is not None:
        query = query.where(MatchGroup.festival_id == festival_id)
    if vendor_id is not None:
        query = query.where(MatchGroup.vendor_id == vendor_id)
    if run_id is not None:
        query = query.where(MatchGroup.run_id == run_id)
    if match_type is not None:
        query = query.where(MatchGroup.match_type == match_type)
    wanted_status = status if status is not None else status_
    if wanted_status is not None:
        stored = _GROUP_STATUSES.get(wanted_status)
        if stored is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "status_unknown",
                    "details": {"status": wanted_status, "accepted": sorted(_GROUP_STATUSES)},
                },
            )
        query = query.where(MatchGroup.status == stored)
    if business_date is not None:
        query = query.where(MatchGroup.business_date == business_date)
    if cursor is not None:
        after = db.get(MatchGroup, cursor)
        if after is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "cursor_unknown", "details": {"cursor": cursor}},
            )
        query = query.where(
            tuple_(MatchGroup.created_at, MatchGroup.id) > (after.created_at, after.id)
        )
    groups = (
        db.execute(query.order_by(MatchGroup.created_at.asc(), MatchGroup.id.asc()).limit(limit))
        .scalars()
        .all()
    )
    # One members query for the whole page, not one per group: the per-group
    # version issued 201,035 extra round trips on the real festival.
    members_by_group: dict[str, list[MatchGroupMember]] = {g.id: [] for g in groups}
    if groups:
        for m in (
            db.execute(
                select(MatchGroupMember).where(
                    MatchGroupMember.match_group_id.in_(list(members_by_group))
                )
            )
            .scalars()
            .all()
        ):
            members_by_group[m.match_group_id].append(m)
    return [_group_out(g, members_by_group[g.id]) for g in groups]


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
    """One group, fetching its own members. Used by the single-group read."""
    members = (
        db.execute(select(MatchGroupMember).where(MatchGroupMember.match_group_id == group.id))
        .scalars()
        .all()
    )
    return _group_out(group, list(members))


def _group_out(group: MatchGroup, members: list[MatchGroupMember]) -> MatchGroupOut:
    """Serialise a group whose members the caller already has, so a page of
    groups costs one members query instead of one per group."""
    member_out = [
        MatchGroupMemberOut(
            member_kind=m.member_kind,
            member_id=m.member_id,
            role=m.role,
            amount_cents=m.amount_cents,
        )
        for m in members
    ]
    # Sum per side rather than per member: two captures on one order are one
    # side of the comparison, and it is the sides a reader needs to see.
    amounts: dict[str, int] = {}
    kinds: list[str] = []
    for m in members:
        amounts[f"{m.member_kind}.{m.role}"] = (
            amounts.get(f"{m.member_kind}.{m.role}", 0) + m.amount_cents
        )
        if m.member_kind not in kinds:
            kinds.append(m.member_kind)
    return MatchGroupOut(
        id=group.id,
        run_id=group.run_id,
        festival_id=group.festival_id,
        vendor_id=group.vendor_id,
        match_type=group.match_type,
        match_kind=group.match_type,
        business_date=group.business_date,
        matched_amount_cents=group.matched_amount_cents,
        variance_cents=group.variance_cents,
        reason_code=group.reason_code,
        explanation=_explanation(group.reason_code),
        status=group.status,
        case_id=group.case_id,
        members=member_out,
        member_refs=member_out,
        member_kinds=kinds,
        matched_amounts=amounts,
        source_refs=group.source_refs or [],
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
