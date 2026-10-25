"""A09 work holds: the hard blockers a challenge or an undecided vendor
recovery puts on releasing money.

`active_hold_for_payout` is called by app/payout_dispatch.py inside
`release_payout` (after the payout row lock) and `dispatch_payout` (before
the rail is contacted). The challenge command takes the same payout row
locks before inserting its holds, so a concurrent release either commits
first (and the challenge then sees, and plans for, a pending payout) or
reads the committed hold and is blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models_corrections import WorkHold


def active_hold_for_payout(
    db: Session, *, vendor_id: str, payout_id: str, settlement_id: str | None
) -> WorkHold | None:
    clauses = [
        (WorkHold.record_kind == "VENDOR") & (WorkHold.record_id == vendor_id),
        (WorkHold.record_kind == "PAYOUT") & (WorkHold.record_id == payout_id),
    ]
    if settlement_id is not None:
        clauses.append(
            (WorkHold.record_kind == "SETTLEMENT") & (WorkHold.record_id == settlement_id)
        )
    return (
        db.execute(
            select(WorkHold)
            .where(WorkHold.status == "ACTIVE", or_(*clauses))
            .order_by(WorkHold.created_at)
        )
        .scalars()
        .first()
    )


def active_vendor_hold(db: Session, vendor_id: str) -> WorkHold | None:
    return (
        db.execute(
            select(WorkHold)
            .where(
                WorkHold.status == "ACTIVE",
                WorkHold.record_kind == "VENDOR",
                WorkHold.record_id == vendor_id,
            )
            .order_by(WorkHold.created_at)
        )
        .scalars()
        .first()
    )


def place_hold(
    db: Session,
    *,
    festival_id: str,
    vendor_id: str | None,
    record_kind: str,
    record_id: str,
    reason_code: str,
    source_kind: str,
    source_id: str,
    actor_id: str | None,
) -> WorkHold:
    hold = WorkHold(
        festival_id=festival_id,
        vendor_id=vendor_id,
        record_kind=record_kind,
        record_id=record_id,
        reason_code=reason_code,
        source_kind=source_kind,
        source_id=source_id,
        status="ACTIVE",
        created_by=actor_id,
    )
    db.add(hold)
    db.flush()
    return hold


def release_holds(
    db: Session, *, source_kind: str, source_id: str, actor_id: str | None, note: str
) -> list[WorkHold]:
    holds = (
        db.execute(
            select(WorkHold).where(
                WorkHold.source_kind == source_kind,
                WorkHold.source_id == source_id,
                WorkHold.status == "ACTIVE",
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for h in holds:
        h.status = "RELEASED"
        h.released_at = now
        h.released_by = actor_id
        h.release_note = note
    db.flush()
    return list(holds)
