"""R1 (prevention extension): readiness endpoints.

`POST /api/v1/readiness/recompute` requires `run_readiness` -- same "trigger
a deterministic engine" capability-gating convention as A03's
`POST /reconciliation/runs` / A04's `POST /settlements/calculate`. Read-only
`GET /api/v1/readiness`/`GET /api/v1/readiness/{id}` only require
authentication (same convention as `GET /agents/extractions`/
`GET /agents/claims`), not a dedicated capability -- viewing a computed
readiness result is not itself a privileged action in this codebase's
existing convention (reading cases/approvals is the closer precedent for a
capability-gated read, but readiness's own TICKET_SPEC.md "API additions"
list does not call for one, and R2/R5 are the tickets that build real
decision/dashboard flows on top of this read).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_capability
from app.db import get_db
from app.models import CAPABILITY_RUN_READINESS, User
from app.models_ledger import Vendor
from app.models_readiness import ReadinessCheck
from app.readiness import (
    UnsupportedCounterpartyTypeError,
    compute_readiness,
    latest_readiness_for_festival,
    readiness_view,
)

router = APIRouter(prefix="/api/v1/readiness", tags=["readiness"])


class RecomputeIn(BaseModel):
    festival_id: str
    festival_start: date
    festival_end: date
    counterparty_type: str = "vendor"
    counterparty_id: str | None = None  # None = recompute every vendor in the festival


@router.post("/recompute")
def recompute_readiness(
    body: RecomputeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_READINESS)),
) -> dict:
    if body.counterparty_id is not None:
        vendor = db.get(Vendor, body.counterparty_id)
        if vendor is None or vendor.festival_id != body.festival_id:
            raise HTTPException(status_code=404, detail="vendor not found in this festival")
        vendors = [vendor]
    else:
        vendors = (
            db.query(Vendor)
            .filter(Vendor.festival_id == body.festival_id)
            .order_by(Vendor.name.asc())
            .all()
        )

    results = []
    for vendor in vendors:
        try:
            row, created = compute_readiness(
                db,
                festival_id=body.festival_id,
                counterparty_type=body.counterparty_type,
                counterparty_id=vendor.id,
                vendor=vendor,
                festival_start=body.festival_start,
                festival_end=body.festival_end,
                actor_id=user.id,
                actor_capability=CAPABILITY_RUN_READINESS,
            )
        except UnsupportedCounterpartyTypeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        results.append({"created_new_version": created, **readiness_view(row)})
        # One commit per counterparty -- a failure partway through a
        # festival-wide recompute never rolls back counterparties already
        # written, same fault-isolation convention as
        # app.policy.detect_contract_term_conflicts_for_festival.
        db.commit()
    return {"festival_id": body.festival_id, "counterparties": results}


@router.get("")
def list_readiness(
    festival_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    rows = latest_readiness_for_festival(db, festival_id=festival_id)
    return {"festival_id": festival_id, "counterparties": [readiness_view(r) for r in rows]}


@router.get("/{readiness_check_id}")
def get_readiness(
    readiness_check_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = db.get(ReadinessCheck, readiness_check_id)
    if row is None:
        raise HTTPException(status_code=404, detail="readiness check not found")
    return readiness_view(row)
