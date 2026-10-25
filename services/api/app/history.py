"""A09 flight recorder reads: event envelopes, historical ("as known at
time T") projections, snapshots and the projection rebuild check.

Two clocks, kept apart (C01/C06):
  - knowledge time = `business_events.recorded_at` (when the system learned
    something). `knowledge_cutoff` filters on it.
  - business time = the festival-local business date / `occurred_at` (when
    it took effect). `business_date` filters on it, separately.

A historical projection is `domain.history.fold` over the festival's
production events recorded at or before the cutoff, optionally resumed
from a stored snapshot. Facts recorded AFTER the cutoff but effective at
or before it are listed separately (`recorded_later`), never merged, so a
past view never silently changes when a later correction arrives.

Snapshots (C06: "stream cursor, projection schema version and state hash;
explicitly derived and rebuildable") are stored in A02's
`projection_snapshots`. `as_of_sequence` is the number of festival events
the snapshot folded. Before a snapshot is reused, its event count is
recounted and its state re-hashed; a mismatch (e.g. a transaction that
began before the snapshot cutoff committed after it) rejects the snapshot
and the projection is folded from the start instead.

Everything here only reads, except `take_snapshot`, which writes a derived
snapshot row (never business data).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from domain import history as dh
from domain.corrections import canonical_hash
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import User
from app.models_corrections import VendorRecovery
from app.models_finance import VendorStatement
from app.models_ledger import (
    BusinessEvent,
    Case,
    EventLineage,
    JournalEntry,
    JournalLine,
    Payout,
    PolicyVersion,
    ProjectionSnapshot,
    Vendor,
)
from app.models_settlement import Settlement

FESTIVAL_TZ = ZoneInfo("America/Los_Angeles")

_REF_KIND_TO_RECORD = {
    "settlement": "SETTLEMENT",
    "payout": "PAYOUT",
    "policy_version": "POLICY_VERSION",
    "case": "CASE",
    "journal_entry": "JOURNAL_ENTRY",
    "source_document": "SOURCE_DOCUMENT",
    "source_version": "SOURCE_DOCUMENT",
    "challenge": "CHALLENGE",
    "correction": "CORRECTION",
    "branch": "BRANCH",
    "replay": "REPLAY",
    "vendor": "VENDOR",
    "run": "RUN",
    "vendor_statement": "REPORT",
    "recovery": "PAYOUT",
    "vendor_contract_term": "CONTRACT_TERM",
}


# ---------------------------------------------------------------------------
# Festival scoping
# ---------------------------------------------------------------------------


def festival_policy_streams(db: Session, festival_id: str) -> list[str]:
    rows = db.execute(select(PolicyVersion.policy_id, PolicyVersion.scope)).all()
    return sorted(
        {f"policy:{pid}" for pid, scope in rows if (scope or {}).get("festival_id") == festival_id}
    )


def festival_event_clause(db: Session, festival_id: str):
    vendor_ids = select(Vendor.id).where(Vendor.festival_id == festival_id)
    streams = festival_policy_streams(db, festival_id) + [
        f"journal:{festival_id}",
        f"cash_pool:{festival_id}",
    ]
    return or_(
        BusinessEvent.vendor_id.in_(vendor_ids),
        BusinessEvent.stream_id.in_(streams),
        BusinessEvent.stream_id.like(f"daily_close:{festival_id}%"),
    )


def festival_of_vendor(db: Session, vendor_id: str) -> str | None:
    v = db.get(Vendor, vendor_id)
    return v.festival_id if v else None


# ---------------------------------------------------------------------------
# Event envelopes (lane B draft EventEnvelope, Q46/Q47)
# ---------------------------------------------------------------------------


def _as_dict(e: BusinessEvent) -> dict:
    return {
        "event_id": e.event_id,
        "event_type": e.event_type,
        "stream_id": e.stream_id,
        "aggregate_id": e.aggregate_id,
        "aggregate_version": e.aggregate_version,
        "payload": e.payload or {},
        "vendor_id": e.vendor_id,
        "branch_id": e.branch_id,
        "recorded_at": e.recorded_at,
        "occurred_at": e.occurred_at,
        "global_position": e.global_position,
        "actor_type": e.actor_type,
    }


def envelope(db: Session, e: BusinessEvent, *, with_links: bool = True) -> dict:
    payload = e.payload or {}
    bd = dh.event_business_date(payload)
    links = []
    if with_links:
        for ln in db.execute(
            select(EventLineage).where(EventLineage.event_id == e.event_id)
        ).scalars():
            links.append(
                {
                    "kind": _REF_KIND_TO_RECORD.get(ln.ref_kind, ln.ref_kind.upper()),
                    "id": ln.ref_id,
                    "version": ln.ref_version,
                    "label": ln.ref_kind,
                }
            )
    actor_label = None
    if e.actor_id:
        user = db.get(User, e.actor_id)
        actor_label = user.email if user is not None else None
    versions = e.versions or {}
    policy_ids = [ln["id"] for ln in links if ln["kind"] == "POLICY_VERSION"] or versions.get(
        "policy_version_ids", []
    )
    if payload.get("applied_policy_version_id"):
        policy_ids = sorted(set(policy_ids) | {payload["applied_policy_version_id"]})
    input_refs = []
    for ref in payload.get("superseding_evidence") or []:
        if isinstance(ref, dict) and ref.get("source_document_id"):
            input_refs.append(
                {
                    "source_id": ref.get("source_document_id"),
                    "source_version": ref.get("source_version"),
                    "content_hash": ref.get("content_hash"),
                    "locator_kind": ref.get("locator_kind"),
                    "locator": ref.get("locator"),
                }
            )
    return {
        "event_id": e.event_id,
        "type": e.event_type,
        "schema_version": e.schema_version,
        "cursor": str(e.global_position),
        "stream_sequence": e.sequence,
        "global_position": e.global_position,
        "stream_id": e.stream_id,
        "aggregate_id": e.aggregate_id,
        "aggregate_version": e.aggregate_version,
        "aggregate_type": dh.aggregate_kind(e.stream_id, e.event_type),
        "run_id": e.run_id,
        "vendor_id": e.vendor_id,
        "branch_id": e.branch_id,
        "occurred_at": e.occurred_at.astimezone(UTC).isoformat(),
        "recorded_at": e.recorded_at.astimezone(UTC).isoformat(),
        "causation_id": e.causation_id,
        "correlation_id": e.correlation_id,
        "command_id": e.command_id,
        "actor_ref": e.actor_id,
        "actor_id": e.actor_id,
        "actor_type": dh.actor_type_label(e.actor_type),
        "actor_label": actor_label,
        "capability": e.actor_capability,
        "category": dh.categorize(e.event_type, e.actor_type),
        "summary": dh.summarize(e.event_type, payload),
        "business_date": bd.isoformat() if bd else None,
        "backdated": dh.is_backdated(
            recorded_at=e.recorded_at,
            occurred_at=e.occurred_at,
            business_date=bd,
            local_date=e.recorded_at.astimezone(FESTIVAL_TZ).date(),
        ),
        "versions": {
            "model": versions.get("model"),
            "prompt": versions.get("prompt"),
            "tool": versions.get("tool"),
            "config": versions.get("config"),
            "calculation": versions.get("calculation") or payload.get("calculation_version"),
            "posting_rule": versions.get("posting_rule"),
            "policy_version_ids": policy_ids,
        },
        "input_refs": input_refs,
        "links": links,
        "previous_hash": e.previous_hash,
        "hash": e.hash,
        "payload": payload,
    }


_AGGREGATE_TYPE_PREFIX = {
    "SETTLEMENT": "settlement:",
    "PAYOUT": "payout:",
    "POLICY_VERSION": "policy:",
    "CASE": "case:",
    "JOURNAL_ENTRY": "journal:",
    "CHALLENGE": "challenge:",
    "CORRECTION": "correction:",
    "BRANCH": "branch:",
    "REPLAY": "replay:",
    "REPORT": "vendor_statement:",
    "VENDOR": "vendor:",
}


def list_events(
    db: Session,
    *,
    festival_id: str | None,
    run_id: str | None = None,
    vendor_id: str | None = None,
    aggregate_id: str | None = None,
    aggregate_type: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    event_type: str | None = None,
    category: str | None = None,
    branch_id: str | None = None,
    correlation_id: str | None = None,
    recorded_from: datetime | None = None,
    recorded_to: datetime | None = None,
    up_to_sequence: int | None = None,
    after_sequence: int | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """Newest first by global position (lane B DA55). Without `branch_id`
    only production events are returned. `up_to_sequence`/`after_sequence`
    and `cursor` are global positions (B's draft calls them sequences)."""
    q = select(BusinessEvent)
    if branch_id is None:
        q = q.where(BusinessEvent.branch_id.is_(None))
    else:
        q = q.where(BusinessEvent.branch_id == branch_id)
    if festival_id:
        q = q.where(festival_event_clause(db, festival_id))
    if run_id:
        q = q.where(BusinessEvent.run_id == run_id)
    if vendor_id:
        q = q.where(BusinessEvent.vendor_id == vendor_id)
    if aggregate_id:
        q = q.where(BusinessEvent.aggregate_id == aggregate_id)
    if aggregate_type and aggregate_type in _AGGREGATE_TYPE_PREFIX:
        q = q.where(BusinessEvent.stream_id.like(_AGGREGATE_TYPE_PREFIX[aggregate_type] + "%"))
    if actor_type:
        q = q.where(func.upper(BusinessEvent.actor_type) == actor_type.upper())
    if actor_id:
        q = q.where(BusinessEvent.actor_id == actor_id)
    if event_type:
        q = q.where(BusinessEvent.event_type == event_type)
    if correlation_id:
        q = q.where(BusinessEvent.correlation_id == correlation_id)
    if recorded_from:
        q = q.where(BusinessEvent.recorded_at >= recorded_from)
    if recorded_to:
        q = q.where(BusinessEvent.recorded_at <= recorded_to)
    if up_to_sequence is not None:
        q = q.where(BusinessEvent.global_position <= up_to_sequence)
    if after_sequence is not None:
        q = q.where(BusinessEvent.global_position > after_sequence)
    head = db.execute(q.with_only_columns(func.max(BusinessEvent.global_position))).scalar()
    if cursor and cursor.isdigit():
        q = q.where(BusinessEvent.global_position < int(cursor))
    limit = max(1, min(limit, 500))
    items: list[dict] = []
    last_pos = None
    scanned = 0
    batch_q = q.order_by(BusinessEvent.global_position.desc())
    offset = 0
    while len(items) < limit and scanned < 20_000:
        rows = db.execute(batch_q.offset(offset).limit(500)).scalars().all()
        if not rows:
            last_pos = None
            break
        offset += len(rows)
        for e in rows:
            scanned += 1
            if category and dh.categorize(e.event_type, e.actor_type) != category:
                continue
            items.append(envelope(db, e))
            last_pos = e.global_position
            if len(items) >= limit:
                break
        if len(rows) < 500 and len(items) < limit:
            last_pos = None
            break
    return {
        "items": items,
        "next_cursor": str(last_pos) if last_pos is not None and len(items) >= limit else None,
        "head_sequence": head or 0,
    }


# ---------------------------------------------------------------------------
# Historical projection
# ---------------------------------------------------------------------------


def _festival_events_query(db: Session, festival_id: str):
    return (
        select(BusinessEvent)
        .where(BusinessEvent.branch_id.is_(None), festival_event_clause(db, festival_id))
        .order_by(BusinessEvent.global_position)
    )


def _count_through(db: Session, festival_id: str, cutoff: datetime) -> int:
    return db.execute(
        select(func.count())
        .select_from(BusinessEvent)
        .where(
            BusinessEvent.branch_id.is_(None),
            festival_event_clause(db, festival_id),
            BusinessEvent.recorded_at <= cutoff,
        )
    ).scalar_one()


def _snapshot_stream(festival_id: str) -> str:
    return f"festival:{festival_id}"


def take_snapshot(db: Session, *, festival_id: str, knowledge_cutoff: datetime) -> dict:
    """Derived and rebuildable: fold from scratch and store the result.
    Never business data; deleting every snapshot loses nothing."""
    events = (
        db.execute(
            _festival_events_query(db, festival_id).where(
                BusinessEvent.recorded_at <= knowledge_cutoff
            )
        )
        .scalars()
        .all()
    )
    state = dh.fold([_as_dict(e) for e in events])
    count = len(events)
    existing = db.execute(
        select(ProjectionSnapshot).where(
            ProjectionSnapshot.projection_name == dh.PROJECTION_NAME,
            ProjectionSnapshot.stream_id == _snapshot_stream(festival_id),
            ProjectionSnapshot.as_of_sequence == count,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return snapshot_ref(existing)
    snap = ProjectionSnapshot(
        projection_name=dh.PROJECTION_NAME,
        stream_id=_snapshot_stream(festival_id),
        as_of_sequence=count,
        schema_version=dh.PROJECTION_SCHEMA_VERSION,
        state={"knowledge_cutoff": knowledge_cutoff.astimezone(UTC).isoformat(), "fold": state},
        state_hash=canonical_hash(state),
    )
    db.add(snap)
    db.flush()
    return snapshot_ref(snap)


def snapshot_ref(s: ProjectionSnapshot) -> dict:
    return {
        "snapshot_id": s.id,
        "stream_cursor": str(s.as_of_sequence),
        "projection_schema_version": s.schema_version,
        "state_hash": s.state_hash,
        "taken_at": (s.state or {}).get("knowledge_cutoff"),
        "computed_at": s.computed_at.isoformat() if s.computed_at else None,
        "events_folded": s.as_of_sequence,
    }


def _usable_snapshot(
    db: Session, festival_id: str, cutoff: datetime
) -> tuple[ProjectionSnapshot | None, str | None]:
    snaps = (
        db.execute(
            select(ProjectionSnapshot)
            .where(
                ProjectionSnapshot.projection_name == dh.PROJECTION_NAME,
                ProjectionSnapshot.stream_id == _snapshot_stream(festival_id),
                ProjectionSnapshot.schema_version == dh.PROJECTION_SCHEMA_VERSION,
            )
            .order_by(ProjectionSnapshot.as_of_sequence.desc())
        )
        .scalars()
        .all()
    )
    for s in snaps:
        snap_cutoff = datetime.fromisoformat(s.state["knowledge_cutoff"])
        if snap_cutoff > cutoff:
            continue
        if canonical_hash(s.state["fold"]) != s.state_hash:
            return None, f"snapshot {s.id} state hash mismatch; folded from the start"
        if _count_through(db, festival_id, snap_cutoff) != s.as_of_sequence:
            return None, (
                f"snapshot {s.id} event count changed since it was taken (a late-committing "
                "transaction); folded from the start"
            )
        return s, None
    return None, None


def historical_projection(
    db: Session,
    *,
    festival_id: str,
    knowledge_cutoff: datetime,
    business_date: date | None,
    vendor_id: str | None,
) -> dict:
    snap, rejected = _usable_snapshot(db, festival_id, knowledge_cutoff)
    q = _festival_events_query(db, festival_id).where(BusinessEvent.recorded_at <= knowledge_cutoff)
    start = None
    if snap is not None:
        snap_cutoff = datetime.fromisoformat(snap.state["knowledge_cutoff"])
        q = q.where(BusinessEvent.recorded_at > snap_cutoff)
        start = snap.state["fold"]
    events = db.execute(q).scalars().all()
    state = dh.fold([_as_dict(e) for e in events], start=start)
    rows = dh.rows(state, business_date=business_date, vendor_id=vendor_id)
    for r in rows:
        if r.get("recorded_at") and r.get("occurred_at"):
            rec = datetime.fromisoformat(r["recorded_at"])
            occ = datetime.fromisoformat(r["occurred_at"])
            bd = date.fromisoformat(r["business_date"]) if r.get("business_date") else None
            r["backdated"] = dh.is_backdated(
                recorded_at=rec,
                occurred_at=occ,
                business_date=bd,
                local_date=rec.astimezone(FESTIVAL_TZ).date(),
            )
    cutoff_local = knowledge_cutoff.astimezone(FESTIVAL_TZ).date()
    later_q = (
        _festival_events_query(db, festival_id)
        .where(BusinessEvent.recorded_at > knowledge_cutoff)
        .limit(2000)
    )
    if vendor_id:
        later_q = later_q.where(
            or_(BusinessEvent.vendor_id == vendor_id, BusinessEvent.vendor_id.is_(None))
        )
    recorded_later = []
    for e in db.execute(later_q).scalars():
        bd = dh.event_business_date(e.payload)
        effective_before = e.occurred_at <= knowledge_cutoff or (
            bd is not None and bd <= cutoff_local
        )
        if not effective_before:
            continue
        if business_date is not None and bd is not None and bd != business_date:
            continue
        recorded_later.append(
            {
                "event_id": e.event_id,
                "type": e.event_type,
                "summary": dh.summarize(e.event_type, e.payload),
                "category": dh.categorize(e.event_type, e.actor_type),
                "record": {
                    "kind": dh.aggregate_kind(e.stream_id, e.event_type),
                    "id": e.aggregate_id,
                    "version": e.aggregate_version,
                    "label": None,
                },
                "business_date": bd.isoformat() if bd else None,
                "occurred_at": e.occurred_at.astimezone(UTC).isoformat(),
                "recorded_at": e.recorded_at.astimezone(UTC).isoformat(),
            }
        )
    return {
        "festival_id": festival_id,
        "knowledge_cutoff": knowledge_cutoff.astimezone(UTC).isoformat(),
        "business_date": business_date.isoformat() if business_date else None,
        "vendor_id": vendor_id,
        "branch_id": None,
        "as_of_sequence": state.get("last_position", 0),
        "snapshot": snapshot_ref(snap) if snap is not None else None,
        "snapshot_rejected": rejected,
        "events_applied": len(events),
        "events_folded_total": state.get("events_applied", 0),
        "state_hash": canonical_hash(state),
        "rows": rows,
        "recorded_later": recorded_later,
        "generated_at": datetime.now(UTC).isoformat(),
        "label": "Reconstructed from recorded events (knowledge time); read-only.",
    }


# ---------------------------------------------------------------------------
# Rebuild check: fold(all events) must equal the live tables
# ---------------------------------------------------------------------------


def verify_projection(db: Session, *, festival_id: str) -> dict:
    events = db.execute(_festival_events_query(db, festival_id)).scalars().all()
    state = dh.fold([_as_dict(e) for e in events])
    mismatches: list[dict] = []
    checked = 0

    def cmp(kind: str, rid: str, field: str, live, folded) -> None:
        nonlocal checked
        checked += 1
        if live != folded:
            mismatches.append(
                {"kind": kind, "id": rid, "field": field, "live": live, "projected": folded}
            )

    vendor_ids = select(Vendor.id).where(Vendor.festival_id == festival_id)
    for s in db.execute(select(Settlement).where(Settlement.festival_id == festival_id)).scalars():
        rec = state["settlements"].get(s.id)
        if rec is None:
            mismatches.append({"kind": "SETTLEMENT", "id": s.id, "field": "missing_event"})
            continue
        cmp("SETTLEMENT", s.id, "payout_cents", s.payout_cents, rec.get("payout_cents"))
        cmp(
            "SETTLEMENT",
            s.id,
            "assessed_fee_cents",
            s.assessed_fee_cents,
            rec.get("assessed_fee_cents"),
        )
    for p in db.execute(select(Payout).where(Payout.vendor_id.in_(vendor_ids))).scalars():
        rec = state["payouts"].get(p.id)
        if rec is None:
            mismatches.append({"kind": "PAYOUT", "id": p.id, "field": "missing_event"})
            continue
        cmp("PAYOUT", p.id, "state", p.state, rec.get("state"))
        cmp("PAYOUT", p.id, "amount_cents", p.amount_cents, rec.get("amount_cents"))
    for pv in db.execute(select(PolicyVersion)).scalars():
        if (pv.scope or {}).get("festival_id") != festival_id:
            continue
        rec = state["policies"].get(pv.id)
        cmp("POLICY_VERSION", pv.id, "status", pv.status, rec.get("status") if rec else None)
    for c in db.execute(select(Case).where(Case.vendor_id.in_(vendor_ids))).scalars():
        rec = state["cases"].get(c.id)
        cmp("CASE", c.id, "status", c.status, rec.get("status") if rec else None)
    debit_rows = db.execute(
        select(JournalEntry.id, func.coalesce(func.sum(JournalLine.debit_cents), 0))
        .join(JournalLine, JournalLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.festival_id == festival_id)
        .group_by(JournalEntry.id)
    ).all()
    for jid, debits in debit_rows:
        rec = state["journal"].get(jid)
        cmp(
            "JOURNAL_ENTRY",
            jid,
            "debit_cents",
            int(debits),
            rec.get("debit_cents") if rec else None,
        )
    for r in db.execute(
        select(VendorRecovery).where(VendorRecovery.festival_id == festival_id)
    ).scalars():
        rec = state["recoveries"].get(r.id)
        cmp("RECOVERY", r.id, "status", r.status, rec.get("status") if rec else None)
    statements = db.execute(
        select(func.count())
        .select_from(VendorStatement)
        .where(VendorStatement.festival_id == festival_id)
    ).scalar_one()
    cmp("REPORT", "vendor_statements", "count", statements, len(state["statements"]))
    return {
        "festival_id": festival_id,
        "events_folded": len(events),
        "records_checked": checked,
        "mismatches": mismatches,
        "rebuild_matches_live_tables": not mismatches,
        "state_hash": canonical_hash(state),
    }


def events_between(
    db: Session, *, festival_id: str, after: datetime | None, through: datetime | None
) -> list[BusinessEvent]:
    q = _festival_events_query(db, festival_id)
    conds = []
    if after is not None:
        conds.append(BusinessEvent.recorded_at > after)
    if through is not None:
        conds.append(BusinessEvent.recorded_at <= through)
    if conds:
        q = q.where(and_(*conds))
    return list(db.execute(q).scalars().all())
