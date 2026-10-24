"""Explicitly rebuildable mutable projection: "latest state per aggregate".

C06: "Snapshots... explicitly derived and rebuildable" and
TASK_IMPLEMENTATION.md A02: "mutable projections explicitly rebuildable."

The rule this module enforces: `AggregateProjection.state` must always be
reproducible by folding `apply_source_event` over `business_events` for a
stream_id, in sequence order, starting from `{}`. There is exactly one
projection type implemented in A02 -- "latest known state of a
source_document" -- used to prove the rebuild story end to end
(ingest -> event -> projection; then wipe the projection row and rebuild it
from the event log alone and get the identical state back). Later tasks add
more projection types; they must follow the same fold-over-events shape, not
invent a second mechanism.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_ledger import AggregateProjection, BusinessEvent

SOURCE_DOCUMENT_PROJECTION_SCHEMA_VERSION = 1


def apply_source_event(state: dict, event: BusinessEvent) -> dict:
    """Pure fold function for the 'source_document' projection type. Given
    the projection state *before* `event` and that event, returns the state
    *after* it. Must never read anything except its own two arguments --
    that purity is what makes replay-from-scratch and live-incremental
    update guaranteed to agree."""
    new_state = dict(state)
    if event.event_type in ("source.imported", "source.quarantined"):
        payload = event.payload or {}
        new_state["latest_version"] = payload.get("version")
        new_state["latest_content_hash"] = payload.get("content_hash")
        new_state["latest_status"] = (
            "quarantined" if event.event_type == "source.quarantined" else "active"
        )
        new_state["versions_seen"] = sorted(
            set(new_state.get("versions_seen", [])) | {payload.get("version")}
        )
    return new_state


def upsert_projection_after_event(db: Session, event: BusinessEvent) -> AggregateProjection:
    """Live, incremental update: apply exactly the one new event to the
    existing projection row (or create it). Called inside the same
    transaction as the event append, so the projection never observes an
    event the event log itself doesn't have committed (atomicity via the
    same Session/transaction as app/uow.py)."""
    existing = db.get(AggregateProjection, event.aggregate_id)
    prior_state = existing.state if existing is not None else {}
    new_state = apply_source_event(prior_state, event)

    if existing is None:
        existing = AggregateProjection(
            aggregate_id=event.aggregate_id,
            aggregate_type="source_document",
            stream_id=event.stream_id,
            latest_sequence=event.sequence,
            latest_event_id=event.event_id,
            state=new_state,
            projection_schema_version=SOURCE_DOCUMENT_PROJECTION_SCHEMA_VERSION,
        )
        db.add(existing)
    else:
        existing.latest_sequence = event.sequence
        existing.latest_event_id = event.event_id
        existing.state = new_state
    db.flush()
    return existing


def rebuild_aggregate_projection(
    db: Session, *, stream_id: str, aggregate_id: str
) -> AggregateProjection:
    """Recompute an AggregateProjection row from scratch by replaying every
    `business_events` row for `stream_id` in sequence order. This is the
    concrete 'rebuild' proof: delete/corrupt the projection row, call this,
    and the state must come back identical to what live incremental updates
    produced (see services/api/tests/test_projection_rebuild.py)."""
    events = (
        db.execute(
            select(BusinessEvent)
            .where(BusinessEvent.stream_id == stream_id)
            .order_by(BusinessEvent.sequence.asc())
        )
        .scalars()
        .all()
    )

    state: dict = {}
    latest_sequence = 0
    latest_event_id: str | None = None
    for event in events:
        state = apply_source_event(state, event)
        latest_sequence = event.sequence
        latest_event_id = event.event_id

    existing = db.get(AggregateProjection, aggregate_id)
    if existing is None:
        existing = AggregateProjection(
            aggregate_id=aggregate_id,
            aggregate_type="source_document",
            stream_id=stream_id,
            latest_sequence=latest_sequence,
            latest_event_id=latest_event_id,
            state=state,
            projection_schema_version=SOURCE_DOCUMENT_PROJECTION_SCHEMA_VERSION,
        )
        db.add(existing)
    else:
        existing.stream_id = stream_id
        existing.latest_sequence = latest_sequence
        existing.latest_event_id = latest_event_id
        existing.state = state
        existing.projection_schema_version = SOURCE_DOCUMENT_PROJECTION_SCHEMA_VERSION
    db.flush()
    return existing
