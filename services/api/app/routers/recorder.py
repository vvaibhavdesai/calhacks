"""A09 flight-recorder reads: events, historical projections, snapshots,
the projection rebuild check, and lineage.

Reads require an authenticated user (same as A08's finance reads); taking
a snapshot (a derived write) requires `run_replay`. Nothing here changes
business data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.history as history
import app.lineage as lineage
from app.auth import get_current_user, require_capability
from app.commands import error
from app.db import get_db
from app.models import CAPABILITY_REPLAY, User
from app.models_ledger import BusinessEvent, ProjectionSnapshot

router = APIRouter(prefix="/api/v1", tags=["recorder"])


def _festival(db: Session, festival_id: str | None, vendor_id: str | None) -> str:
    if festival_id:
        return festival_id
    if vendor_id:
        f = history.festival_of_vendor(db, vendor_id)
        if f:
            return f
    raise error(422, "FESTIVAL_REQUIRED", "festival_id (or a known vendor_id) is required")


def _aware(dt: datetime | None, field: str) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        raise error(
            422,
            "TIMEZONE_REQUIRED",
            f"{field} must be an RFC3339 instant with an offset (UTC)",
            field_errors=[{"field": field, "message": "missing offset"}],
        )
    return dt


@router.get("/events")
def list_events(
    festival_id: str | None = None,
    run_id: str | None = None,
    vendor_id: str | None = None,
    aggregate_id: str | None = None,
    aggregate_type: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    type: str | None = None,  # noqa: A002 - lane B's draft query name
    category: str | None = None,
    branch_id: str | None = None,
    correlation_id: str | None = None,
    recorded_from: datetime | None = None,
    recorded_to: datetime | None = None,
    up_to_sequence: int | None = Query(default=None, ge=0),
    after_sequence: int | None = Query(default=None, ge=0),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if category and category not in ("FACT", "PROPOSAL", "INTENT", "EFFECT", "PROJECTION"):
        raise error(422, "INVALID_CATEGORY", "unknown category")
    return history.list_events(
        db,
        festival_id=festival_id,
        run_id=run_id,
        vendor_id=vendor_id,
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=type,
        category=category,
        branch_id=branch_id,
        correlation_id=correlation_id,
        recorded_from=_aware(recorded_from, "recorded_from"),
        recorded_to=_aware(recorded_to, "recorded_to"),
        up_to_sequence=up_to_sequence,
        after_sequence=after_sequence,
        cursor=cursor,
        limit=limit,
    )


@router.get("/events/{event_id}")
def get_event(
    event_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    e = db.get(BusinessEvent, event_id)
    if e is None:
        raise HTTPException(status_code=404, detail="event not found")
    return history.envelope(db, e)


@router.get("/history/projection")
def get_historical_projection(
    # "As known at" defaults to now, which is the projection a reader wants
    # before they have chosen a point in the past. It was required, so the
    # screen could not ask the question at all without inventing a cutoff.
    knowledge_cutoff: datetime | None = None,
    business_date: date | None = None,
    vendor_id: str | None = None,
    festival_id: str | None = None,
    branch_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if knowledge_cutoff is None:
        knowledge_cutoff = datetime.now(UTC)
    if branch_id:
        raise error(
            422,
            "BRANCH_HISTORY_UNSUPPORTED",
            "branch history is served by GET /branches/{id}; this projection is production only",
        )
    fid = _festival(db, festival_id, vendor_id)
    return history.historical_projection(
        db,
        festival_id=fid,
        knowledge_cutoff=_aware(knowledge_cutoff, "knowledge_cutoff"),
        business_date=business_date,
        vendor_id=vendor_id,
    )


@router.post("/history/snapshots", status_code=201)
def take_snapshot(
    festival_id: str,
    knowledge_cutoff: datetime,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_REPLAY)),
) -> dict:
    ref = history.take_snapshot(
        db, festival_id=festival_id, knowledge_cutoff=_aware(knowledge_cutoff, "knowledge_cutoff")
    )
    db.commit()
    return ref


@router.get("/history/snapshots")
def list_snapshots(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(
        select(ProjectionSnapshot)
        .where(ProjectionSnapshot.stream_id == f"festival:{festival_id}")
        .order_by(ProjectionSnapshot.as_of_sequence)
    ).scalars()
    return {"items": [history.snapshot_ref(s) for s in rows], "next_cursor": None}


@router.get("/history/verify")
def verify_projection(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return history.verify_projection(db, festival_id=festival_id)


@router.get("/lineage/tax-total")
def lineage_tax_total(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return lineage.trace_tax_total(db, festival_id=festival_id)


@router.get("/lineage/{kind}/{record_id}")
def lineage_trace(
    kind: str,
    record_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return lineage.trace(db, kind=kind, record_id=record_id)
    except lineage.LineageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise error(422, "UNSUPPORTED_KIND", str(exc)) from exc
