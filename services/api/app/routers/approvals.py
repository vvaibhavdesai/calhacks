"""X0.1 generic Approval endpoint.

Unlike every prior router's capability check (`require_capability(<a
literal code baked in at route-registration time>)`), this endpoint's
required capability is DATA-DRIVEN: it depends on the request body's
`action` field (domain.approvals.required_capability_for_action). A
FastAPI dependency factory like `require_capability` cannot express "the
exact capability needed depends on a field in the request body" -- so this
endpoint authenticates via `get_current_user` (same 401-before-403
ordering as everywhere else) and then performs the SAME underlying check
`require_capability` performs (`app.auth.get_user_capability_codes`,
re-read from the database every request, never trusted from the token),
just with a capability code resolved at request time instead of import
time. This is not a parallel auth mechanism -- it is the same primitive,
applied dynamically.
"""

from __future__ import annotations

from domain.approvals import required_capability_for_action
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals import ApprovalConflictError, ApprovalNotFoundError, record_approval
from app.auth import get_current_user, get_user_capability_codes, require_capability
from app.db import get_db
from app.models import CAPABILITY_RESOLVE_CASE, User
from app.models_ledger import Approval
from app.uow import UnitOfWork

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    case_id: str | None
    action: str
    required_capability: str
    note: str | None
    payload: dict
    actor_id: str | None
    actor_capability: str


def _to_approval_out(a: Approval) -> ApprovalOut:
    return ApprovalOut(
        id=a.id,
        subject_type=a.subject_type,
        subject_id=a.subject_id,
        case_id=a.case_id,
        action=a.action,
        required_capability=a.required_capability,
        note=a.note,
        payload=a.payload,
        actor_id=a.actor_id,
        actor_capability=a.actor_capability,
    )


class CreateApprovalRequest(BaseModel):
    subject_type: str
    subject_id: str
    action: str
    case_id: str | None = None
    note: str | None = None
    payload: dict | None = None


@router.post("", response_model=ApprovalOut, status_code=201)
def create_approval(
    body: CreateApprovalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalOut:
    try:
        required_capability = required_capability_for_action(body.action)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # The actual enforced check -- 403 if the real, DB-reread capability
    # set for this user doesn't include what this action requires. Same
    # primitive app.auth.require_capability uses, applied to a capability
    # resolved from the request body rather than hardcoded at decoration
    # time (see module docstring).
    held_codes = get_user_capability_codes(db, user.id)
    if required_capability not in held_codes:
        raise HTTPException(
            status_code=403,
            detail=f"missing required capability: {required_capability}",
        )

    uow = UnitOfWork(db)
    try:
        approval = record_approval(
            db,
            uow,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            action=body.action,
            case_id=body.case_id,
            note=body.note,
            payload=body.payload,
            actor_id=user.id,
            actor_capability=required_capability,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, **exc.details}) from exc
    # Exactly one commit point (C06), same convention as every other
    # command endpoint in this codebase.
    db.commit()
    return _to_approval_out(approval)


@router.get("", response_model=list[ApprovalOut])
def list_approvals(
    subject_type: str | None = None,
    subject_id: str | None = None,
    case_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> list[ApprovalOut]:
    query = select(Approval)
    if subject_type is not None:
        query = query.where(Approval.subject_type == subject_type)
    if subject_id is not None:
        query = query.where(Approval.subject_id == subject_id)
    if case_id is not None:
        query = query.where(Approval.case_id == case_id)
    rows = db.execute(query.order_by(Approval.created_at.asc())).scalars().all()
    return [_to_approval_out(a) for a in rows]


@router.get("/{approval_id}", response_model=ApprovalOut)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RESOLVE_CASE)),
) -> ApprovalOut:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return _to_approval_out(approval)
