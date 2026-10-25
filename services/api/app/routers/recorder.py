"""A09 flight-recorder reads: events, historical projections, snapshots,
the projection rebuild check, and lineage.

Reads require an authenticated user (same as A08's finance reads); taking
a snapshot (a derived write) requires `run_replay`. Nothing here changes
business data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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


# Instants are `str`: the handlers emit `.isoformat()` (offset `+00:00`),
# and a `datetime` field would make pydantic rewrite that to `...Z`.


class EventLinkOut(BaseModel):
    """A record this event touched (app/history.py `envelope`)."""

    kind: str
    id: str
    version: int | None
    label: str


class EventInputRefOut(BaseModel):
    source_id: str | None
    source_version: int | None
    content_hash: str | None
    locator_kind: str | None
    locator: str | None


class EventVersionsOut(BaseModel):
    """Which versioned thing produced this event. Null means "not applicable
    to this event type", not "version zero"."""

    model: str | None
    prompt: str | None
    tool: str | None
    config: str | None
    calculation: str | None
    posting_rule: str | None
    policy_version_ids: list[str]


class EventEnvelopeOut(BaseModel):
    event_id: str
    type: str
    schema_version: int
    cursor: str
    stream_sequence: int
    global_position: int
    stream_id: str
    aggregate_id: str
    aggregate_version: int | None
    aggregate_type: str
    run_id: str | None
    vendor_id: str | None
    branch_id: str | None
    occurred_at: str
    recorded_at: str
    causation_id: str | None
    correlation_id: str | None
    command_id: str | None
    actor_ref: str | None
    actor_id: str | None
    actor_type: str
    # The actor's email, or null for a non-human actor (or a deleted user).
    actor_label: str | None
    capability: str | None
    category: str  # FACT | PROPOSAL | INTENT | EFFECT | PROJECTION
    summary: str
    business_date: str | None
    backdated: bool
    versions: EventVersionsOut
    input_refs: list[EventInputRefOut]
    links: list[EventLinkOut]
    # Null for the first event on a stream: there is no previous hash.
    previous_hash: str | None
    hash: str
    # The recorded event body. Its keys are entirely event-type specific, so
    # modelling them here would drop every key a model did not anticipate.
    payload: dict


class EventListOut(BaseModel):
    items: list[EventEnvelopeOut]
    # Null when this page is the end of the stream.
    next_cursor: str | None
    head_sequence: int


class SnapshotRefOut(BaseModel):
    snapshot_id: str
    stream_cursor: str
    projection_schema_version: int
    state_hash: str
    # The knowledge cutoff the snapshot was taken at, from its stored state.
    taken_at: str | None
    computed_at: str | None
    events_folded: int


class SnapshotListOut(BaseModel):
    items: list[SnapshotRefOut]
    # Always null: a festival's snapshots are a page, not a stream.
    next_cursor: str | None


class ProjectionRowOut(BaseModel):
    """One record as it stood at the knowledge cutoff."""

    label: str
    status: str
    # MONEY AS A STRING. The projection renders amounts already formatted,
    # so this is not an int -- retyping it would change the JSON.
    amount_cents: str | None
    amount_label: str | None
    vendor_id: str | None
    business_date: str | None
    occurred_at: str
    recorded_at: str
    backdated: bool
    note: str | None
    policy_version_id: str | None
    # {kind, id, version, label} for the record this row is about.
    record: dict


class HistoricalProjectionOut(BaseModel):
    festival_id: str
    knowledge_cutoff: str
    business_date: str | None
    vendor_id: str | None
    # Always null here: branch history is served by GET /branches/{id}.
    branch_id: str | None
    as_of_sequence: int
    # The snapshot this fold started from, or null if it folded from zero.
    snapshot: SnapshotRefOut | None
    # Why a candidate snapshot was NOT used, when one was rejected.
    snapshot_rejected: str | None
    events_applied: int
    events_folded_total: int
    state_hash: str
    rows: list[ProjectionRowOut]
    recorded_later: list[dict]
    generated_at: str
    label: str


class ProjectionMismatchOut(BaseModel):
    """One disagreement between the folded events and the live tables.

    `live`/`projected` are absent (not null) on a `missing_event` finding --
    there is no pair of values to compare -- which is why the route
    serialises with `response_model_exclude_unset`."""

    kind: str
    id: str
    field: str
    live: Any = None
    projected: Any = None


class VerifyProjectionOut(BaseModel):
    festival_id: str
    rebuild_matches_live_tables: bool
    records_checked: int
    events_folded: int
    state_hash: str
    mismatches: list[ProjectionMismatchOut]


class LineageEdgeOut(BaseModel):
    """`later=true` points at a record created after the `from` one."""

    # `from` is a Python keyword, so the field is aliased. FastAPI serialises
    # by alias, so the wire name stays "from".
    from_: str = Field(alias="from")
    to: str
    relation: str
    later: bool


class LineageVendorOut(BaseModel):
    id: str
    name: str


class LineageOut(BaseModel):
    """A record traced back to the sources and decisions behind it.

    `nodes`, `sources`, `decisions` and `root_node` are deliberately open
    objects: a node's keys depend on its `kind` (a settlement node carries
    `commission_bps_used`, a source-document node carries `content_hash` and
    `locator`, a journal node carries `lines`). A fixed model would keep the
    handful of keys every kind shares and silently drop the rest -- which is
    most of what makes a lineage graph worth reading."""

    root: str
    root_node: dict
    # Null when the traced record does not belong to one vendor.
    vendor: LineageVendorOut | None
    nodes: list[dict]
    edges: list[LineageEdgeOut]
    sources: list[dict]
    decisions: list[dict]
    human_decisions: list[dict]
    truncated: bool
    note: str


class TaxLineageOut(LineageOut):
    # Only the tax-total trace reports this: it walks a capped number of
    # journal entries, and says so rather than quietly showing a partial sum.
    entries_truncated: bool


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


@router.get("/events", response_model=EventListOut)
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


@router.get("/events/{event_id}", response_model=EventEnvelopeOut)
def get_event(
    event_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    e = db.get(BusinessEvent, event_id)
    if e is None:
        raise HTTPException(status_code=404, detail="event not found")
    return history.envelope(db, e)


@router.get("/history/projection", response_model=HistoricalProjectionOut)
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


@router.post("/history/snapshots", status_code=201, response_model=SnapshotRefOut)
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


@router.get("/history/snapshots", response_model=SnapshotListOut)
def list_snapshots(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(
        select(ProjectionSnapshot)
        .where(ProjectionSnapshot.stream_id == f"festival:{festival_id}")
        .order_by(ProjectionSnapshot.as_of_sequence)
    ).scalars()
    return {"items": [history.snapshot_ref(s) for s in rows], "next_cursor": None}


@router.get(
    "/history/verify", response_model=VerifyProjectionOut, response_model_exclude_unset=True
)
def verify_projection(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return history.verify_projection(db, festival_id=festival_id)


@router.get("/lineage/tax-total", response_model=TaxLineageOut)
def lineage_tax_total(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return lineage.trace_tax_total(db, festival_id=festival_id)


@router.get("/lineage/{kind}/{record_id}", response_model=LineageOut)
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
