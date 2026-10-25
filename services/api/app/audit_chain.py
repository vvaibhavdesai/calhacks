"""Verification of the per-stream audit hash chain written by app/uow.py.

The chain itself has existed since A02; until this module there was
nothing that recomputed it, so the guarantee was asserted but never
exercised. Everything here is READ-ONLY -- it recomputes and compares, and
never repairs, backfills or re-points anything. A divergence is a finding
for a human, not something software should quietly fix: the whole point of
an append-only log is that the log wins.

Three independent properties are checked per stream, and reported
separately so one tampered row produces one finding rather than a cascade:

  1. hash correctness -- `sha256(canonical(event fields incl. the event's
     OWN stored previous_hash)) == event.hash`. Detects any edit to a
     hashed field of that event.
  2. linkage -- `event.previous_hash == prior_event.hash`, with
     `GENESIS_HASH` expected at sequence 1. Detects an event spliced in,
     removed or re-pointed, even if each row's own hash is internally
     consistent.
  3. sequence contiguity -- sequences are 1..n with no gap. UNIQUE
     (stream_id, sequence) already rules out duplicates.

Two depths, because they cost very different amounts:

  - `head_consistency(db)` is pure SQL (three grouped queries, no hashing)
    and cross-checks `stream_heads` against the events that exist. Cheap
    enough to run on every `/api/v1/ops/alerts` and `/metrics` call, which
    is where app/ops.py calls it.
  - `verify_chain(db, ...)` reads and re-hashes every event of every
    stream in scope. That is the real verification, and it is deliberately
    NOT on the alerts path: re-hashing the whole log on a 15-second
    Prometheus scrape would be the monitoring dominating the system it
    monitors. It is reached on demand via `GET /api/v1/ops/audit-chain` or
    `scripts/verify_audit_chain.py`.

`app/replay.py` delegates its own per-stream chain check here rather than
keeping a second copy of the walk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models_ledger import BusinessEvent, StreamHead
from app.uow import GENESIS_HASH, compute_event_hash, event_hash_input

# Divergence kinds. Strings, not an enum, because they are published in
# JSON and on `/metrics` and must stay stable for whoever reads them.
HASH_MISMATCH = "HASH_MISMATCH"
BROKEN_LINK = "BROKEN_LINK"
SEQUENCE_GAP = "SEQUENCE_GAP"

# Head-consistency finding kinds (the cheap SQL-only check).
HEAD_MISSING = "HEAD_MISSING"
HEAD_SEQUENCE_MISMATCH = "HEAD_SEQUENCE_MISMATCH"
HEAD_HASH_MISMATCH = "HEAD_HASH_MISMATCH"
HEAD_WITHOUT_EVENTS = "HEAD_WITHOUT_EVENTS"

# Caps so a catastrophically broken log produces a readable report and a
# bounded JSON body instead of one divergence per event.
DEFAULT_MAX_DIVERGENCES_PER_STREAM = 20
DEFAULT_MAX_DIVERGENCES = 500


def recompute_event_hash(e: BusinessEvent) -> str:
    """The hash `e` must have, recomputed from the stored row.

    Uses the event's OWN `previous_hash`, not the recomputed hash of its
    predecessor, so this answers "was this row edited?" independently of
    "is this row linked to the right predecessor?" (that is `linkage`)."""
    return compute_event_hash(
        event_hash_input(
            event_id=e.event_id,
            stream_id=e.stream_id,
            sequence=e.sequence,
            schema_version=e.schema_version,
            event_type=e.event_type,
            actor_id=e.actor_id,
            actor_type=e.actor_type,
            actor_capability=e.actor_capability,
            occurred_at=e.occurred_at,
            aggregate_id=e.aggregate_id,
            aggregate_version=e.aggregate_version,
            run_id=e.run_id,
            vendor_id=e.vendor_id,
            branch_id=e.branch_id,
            correlation_id=e.correlation_id,
            causation_id=e.causation_id,
            command_id=e.command_id,
            payload=e.payload,
            versions=e.versions,
            previous_hash=e.previous_hash,
        )
    )


@dataclass(frozen=True)
class Divergence:
    """One failed check, pinned to the event it was found on."""

    stream_id: str
    sequence: int
    event_id: str | None
    kind: str
    expected: str | None
    stored: str | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "kind": self.kind,
            "expected": self.expected,
            "stored": self.stored,
            "detail": self.detail,
        }


@dataclass
class StreamReport:
    """Verification result for one stream."""

    stream_id: str
    events: int
    first_sequence: int | None
    last_sequence: int | None
    sequence_contiguous: bool = True
    linkage_intact: bool = True
    hashes_intact: bool = True
    truncated: bool = False  # divergences were capped for this stream
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.sequence_contiguous and self.linkage_intact and self.hashes_intact

    @property
    def first_divergent_sequence(self) -> int | None:
        """The earliest sequence anything went wrong at -- the only number
        worth looking at first, because everything after it is suspect."""
        return min((d.sequence for d in self.divergences), default=None)

    def as_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "ok": self.ok,
            "events": self.events,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "sequence_contiguous": self.sequence_contiguous,
            "linkage_intact": self.linkage_intact,
            "hashes_intact": self.hashes_intact,
            "first_divergent_sequence": self.first_divergent_sequence,
            "divergences_truncated": self.truncated,
            "divergences": [d.as_dict() for d in self.divergences],
        }


@dataclass(frozen=True)
class HeadFinding:
    """One `stream_heads` vs `business_events` inconsistency."""

    stream_id: str
    kind: str
    expected: str | None
    stored: str | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "kind": self.kind,
            "expected": self.expected,
            "stored": self.stored,
            "detail": self.detail,
        }


@dataclass
class ChainReport:
    """Verification result for every stream in scope."""

    checked_at: datetime
    streams_checked: int = 0
    streams_diverged: int = 0
    events_checked: int = 0
    hash_mismatches: int = 0
    broken_links: int = 0
    sequence_gaps: int = 0
    truncated: bool = False  # the global divergence cap was hit
    streams: list[StreamReport] = field(default_factory=list)
    head_findings: list[HeadFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.streams_diverged == 0 and not self.head_findings

    @property
    def divergences(self) -> list[Divergence]:
        return [d for s in self.streams for d in s.divergences]


def _verify_one_stream(
    events: Sequence[BusinessEvent],
    *,
    stream_id: str,
    max_divergences: int = DEFAULT_MAX_DIVERGENCES_PER_STREAM,
) -> StreamReport:
    """Walk one stream's events, in sequence order, and check all three
    properties. Pure: takes the rows, touches no database."""
    report = StreamReport(
        stream_id=stream_id,
        events=len(events),
        first_sequence=events[0].sequence if events else None,
        last_sequence=events[-1].sequence if events else None,
    )

    def add(d: Divergence) -> None:
        if len(report.divergences) < max_divergences:
            report.divergences.append(d)
        else:
            report.truncated = True

    expected_sequence = 1
    # None = "the predecessor is not available, so don't claim a broken
    # link" (only reachable when the stream does not start at 1, which is
    # itself already reported as a gap).
    expected_previous: str | None = GENESIS_HASH
    for e in events:
        if e.sequence != expected_sequence:
            report.sequence_contiguous = False
            add(
                Divergence(
                    stream_id=stream_id,
                    sequence=e.sequence,
                    event_id=e.event_id,
                    kind=SEQUENCE_GAP,
                    expected=str(expected_sequence),
                    stored=str(e.sequence),
                    detail=(
                        f"stream {stream_id}: expected sequence {expected_sequence}, "
                        f"found {e.sequence}"
                    ),
                )
            )
            expected_previous = None

        if expected_previous is not None and e.previous_hash != expected_previous:
            report.linkage_intact = False
            add(
                Divergence(
                    stream_id=stream_id,
                    sequence=e.sequence,
                    event_id=e.event_id,
                    kind=BROKEN_LINK,
                    expected=expected_previous,
                    stored=e.previous_hash,
                    detail=(
                        f"stream {stream_id} sequence {e.sequence}: previous_hash does not "
                        f"match the stored hash of sequence {e.sequence - 1}"
                        if e.sequence > 1
                        else f"stream {stream_id} sequence 1: previous_hash is not GENESIS_HASH"
                    ),
                )
            )

        recomputed = recompute_event_hash(e)
        if recomputed != e.hash:
            report.hashes_intact = False
            add(
                Divergence(
                    stream_id=stream_id,
                    sequence=e.sequence,
                    event_id=e.event_id,
                    kind=HASH_MISMATCH,
                    expected=recomputed,
                    stored=e.hash,
                    detail=(
                        f"stream {stream_id} sequence {e.sequence} ({e.event_type}): "
                        f"recomputed hash does not match the stored hash"
                    ),
                )
            )

        expected_previous = e.hash
        expected_sequence = e.sequence + 1
    return report


def _stream_ids(db: Session, *, stream_prefix: str | None = None) -> list[str]:
    q = select(BusinessEvent.stream_id).distinct().order_by(BusinessEvent.stream_id)
    if stream_prefix:
        q = q.where(BusinessEvent.stream_id.startswith(stream_prefix))
    return list(db.execute(q).scalars().all())


def verify_chain(
    db: Session,
    *,
    stream_ids: Sequence[str] | None = None,
    stream_prefix: str | None = None,
    include_heads: bool = True,
    max_divergences_per_stream: int = DEFAULT_MAX_DIVERGENCES_PER_STREAM,
    max_divergences: int = DEFAULT_MAX_DIVERGENCES,
    now: datetime | None = None,
) -> ChainReport:
    """Recompute and check the chain of every stream in scope.

    `stream_ids` verifies exactly those streams (the shape app/replay.py
    uses, so a replay checks the streams it touched); `stream_prefix`
    narrows by stream-id prefix (e.g. `"settlement:"`); neither means every
    stream that has an event. Streams are read one at a time so a large log
    is not materialized all at once.

    Reads only. Safe inside a `SET TRANSACTION READ ONLY` transaction,
    which is how replay calls it.
    """
    report = ChainReport(checked_at=now or datetime.now(UTC))
    if stream_ids is not None:
        ids = list(stream_ids)
    else:
        ids = _stream_ids(db, stream_prefix=stream_prefix)
    recorded = 0
    for stream_id in sorted(ids):
        events = list(
            db.execute(
                select(BusinessEvent)
                .where(BusinessEvent.stream_id == stream_id)
                .order_by(BusinessEvent.sequence)
            )
            .scalars()
            .all()
        )
        remaining = max(0, max_divergences - recorded)
        if remaining == 0:
            report.truncated = True
        stream = _verify_one_stream(
            events,
            stream_id=stream_id,
            max_divergences=min(max_divergences_per_stream, remaining),
        )
        report.streams.append(stream)
        report.streams_checked += 1
        report.events_checked += stream.events
        if not stream.ok:
            report.streams_diverged += 1
        report.truncated = report.truncated or stream.truncated
        recorded += len(stream.divergences)
        # Counted from the divergences actually recorded, so a report whose
        # `divergences_truncated` is set under-counts rather than lying
        # about the per-stream booleans, which are always exact.
        for d in stream.divergences:
            if d.kind == HASH_MISMATCH:
                report.hash_mismatches += 1
            elif d.kind == BROKEN_LINK:
                report.broken_links += 1
            elif d.kind == SEQUENCE_GAP:
                report.sequence_gaps += 1

    if include_heads:
        report.head_findings = head_consistency(db, stream_ids=ids)
    return report


def head_consistency(db: Session, *, stream_ids: Sequence[str] | None = None) -> list[HeadFinding]:
    """Cross-check `stream_heads` against the events that exist, in SQL
    only -- no event rows are loaded and nothing is re-hashed.

    `stream_heads` is the one legitimately-mutable part of the chain (it is
    a pointer, and `encore_app` can UPDATE it; `business_events` it cannot).
    So this is where a disagreement is cheap to find and worth watching
    continuously: a head that does not name the tip event means either an
    event was appended without going through `UnitOfWork.append_event`, or
    the pointer was moved without an event. It also gets sequence
    contiguity for free: with UNIQUE(stream_id, sequence) and a first
    sequence of 1, `count(*) == max(sequence)` iff there is no gap.
    """
    per_stream = (
        select(
            BusinessEvent.stream_id.label("stream_id"),
            func.count().label("events"),
            func.min(BusinessEvent.sequence).label("first_sequence"),
            func.max(BusinessEvent.sequence).label("last_sequence"),
        )
        .group_by(BusinessEvent.stream_id)
        .subquery()
    )
    tips_q = select(
        per_stream.c.stream_id,
        per_stream.c.events,
        per_stream.c.first_sequence,
        per_stream.c.last_sequence,
        BusinessEvent.hash.label("tip_hash"),
    ).join(
        BusinessEvent,
        (BusinessEvent.stream_id == per_stream.c.stream_id)
        & (BusinessEvent.sequence == per_stream.c.last_sequence),
    )
    heads_q = select(StreamHead)
    if stream_ids is not None:
        ids = list(stream_ids)
        tips_q = tips_q.where(per_stream.c.stream_id.in_(ids))
        heads_q = heads_q.where(StreamHead.stream_id.in_(ids))

    tips = {r.stream_id: r for r in db.execute(tips_q).all()}
    heads = {h.stream_id: h for h in db.execute(heads_q).scalars().all()}

    findings: list[HeadFinding] = []
    for stream_id in sorted(set(tips) | set(heads)):
        tip = tips.get(stream_id)
        head = heads.get(stream_id)
        if tip is None:
            # A head row with no events is only wrong once it claims to
            # point at one (last_sequence 0 is the freshly-inserted
            # placeholder `append_event` creates before it appends).
            if head is not None and head.last_sequence > 0:
                findings.append(
                    HeadFinding(
                        stream_id=stream_id,
                        kind=HEAD_WITHOUT_EVENTS,
                        expected=None,
                        stored=str(head.last_sequence),
                        detail=f"stream_heads claims sequence {head.last_sequence} but the "
                        "stream has no events",
                    )
                )
            continue
        if head is None:
            findings.append(
                HeadFinding(
                    stream_id=stream_id,
                    kind=HEAD_MISSING,
                    expected=str(tip.last_sequence),
                    stored=None,
                    detail=f"{tip.events} event(s) exist but there is no stream_heads row",
                )
            )
        else:
            if head.last_sequence != tip.last_sequence:
                findings.append(
                    HeadFinding(
                        stream_id=stream_id,
                        kind=HEAD_SEQUENCE_MISMATCH,
                        expected=str(tip.last_sequence),
                        stored=str(head.last_sequence),
                        detail="stream_heads.last_sequence does not name the highest event",
                    )
                )
            if head.last_hash != tip.tip_hash:
                findings.append(
                    HeadFinding(
                        stream_id=stream_id,
                        kind=HEAD_HASH_MISMATCH,
                        expected=tip.tip_hash,
                        stored=head.last_hash,
                        detail=f"stream_heads.last_hash is not the hash of sequence "
                        f"{tip.last_sequence}",
                    )
                )
        if tip.first_sequence != 1 or tip.events != tip.last_sequence:
            findings.append(
                HeadFinding(
                    stream_id=stream_id,
                    kind=SEQUENCE_GAP,
                    expected=f"1..{tip.events}",
                    stored=f"{tip.first_sequence}..{tip.last_sequence}",
                    detail=f"{tip.events} event(s) spanning sequence {tip.first_sequence}.."
                    f"{tip.last_sequence}: the sequence is not contiguous from 1",
                )
            )
    return findings


def report_to_json(report: ChainReport, *, include_ok_streams: bool = False) -> dict:
    """JSON for `GET /api/v1/ops/audit-chain`.

    Intact streams are summarized as a count by default: on a healthy log
    that is thousands of identical "ok" entries, and the only thing a
    reader needs from them is that there were no surprises."""
    streams = [s for s in report.streams if include_ok_streams or not s.ok]
    return {
        "status": "ok" if report.ok else "diverged",
        "checked_at": report.checked_at.isoformat(),
        "summary": {
            "streams_checked": report.streams_checked,
            "streams_diverged": report.streams_diverged,
            "events_checked": report.events_checked,
            "hash_mismatches": report.hash_mismatches,
            "broken_links": report.broken_links,
            "sequence_gaps": report.sequence_gaps,
            "head_findings": len(report.head_findings),
            "divergences_truncated": report.truncated,
        },
        "streams": [s.as_dict() for s in streams],
        "head_findings": [f.as_dict() for f in report.head_findings],
    }
